import ast
import copy
import hashlib
import json
from pathlib import Path

import pytest
import glossary_audit as audit_module

from glossary_audit import (
    AuditCheckpointMismatch,
    AuditChunkConfig,
    AuditChunkFailure,
    AuditResultValidationError,
    audit_semantic_correction_authorization,
    audit_candidate_set_hash,
    build_audit_chunks,
    classify_audit_chunk_recovery,
    resolve_audit_candidate_families,
    run_chunked_audit,
    try_deterministic_glossary_semantic_normalization,
    validate_audit_chunk_result,
)
from model_candidate_ids import (
    build_model_candidate_id_map,
    model_facing_candidates,
    resolve_model_candidate_response,
)
from glossary_integrity import (
    build_source_integrity_audit_plan,
    stable_glossary_candidate_id,
)


def candidate(source, english=None, *, alias_of=""):
    return {
        "chinese": source,
        "english": english or f"Term {source}",
        "category": "specialized recurring term",
        "status": "provisional",
        "alias_of": alias_of,
        "contexts": [{"chapter": 1, "context": f"context {source}"}],
    }


def identified_candidate(source, english=None):
    item = candidate(source, english)
    item["candidate_id"] = stable_glossary_candidate_id(source)
    return item


def valid_response(targets, *, finish_reason="stop", stats=None, overrides=None):
    overrides = overrides or {}
    decisions = []
    for item in targets:
        decision = {
            "chinese": item["chinese"],
            "action": "keep",
            "english": item["english"],
            "approved_renderings": [],
            "merge_with": "",
            "notes": "",
        }
        decision.update(overrides.get(item["chinese"], {}))
        decisions.append(decision)
    return {
        "content": json.dumps({"decisions": decisions, "issues": []}),
        "finish_reason": finish_reason,
        "stats": stats or {"prompt_tokens": 10, "completion_tokens": 5, "cost_usd": 0.01},
    }


def valid_identified_response(
    targets, *, finish_reason="stop", stats=None, overrides=None
):
    overrides = overrides or {}
    decisions = []
    for item in targets:
        decision = {
            "candidate_id": item["candidate_id"],
            "action": "keep",
            "english": item["english"],
            "approved_renderings": [],
            "merge_with": "",
            "notes": "",
        }
        decision.update(overrides.get(item["candidate_id"], {}))
        decisions.append(decision)
    return {
        "content": json.dumps({"decisions": decisions, "issues": []}),
        "finish_reason": finish_reason,
        "stats": stats or {"prompt_tokens": 10, "completion_tokens": 5, "cost_usd": 0.01},
    }


def response_for_parsed(parsed, *, stats=None):
    return {
        "content": json.dumps(parsed),
        "finish_reason": "stop",
        "stats": stats or {},
    }


def run_audit(
    tmp_path,
    candidates,
    request,
    *,
    repair=None,
    semantic=None,
    config=None,
    context="v1",
    supersede=False,
):
    return run_chunked_audit(
        candidates=candidates,
        all_glossary_keys=[item["chinese"] for item in candidates],
        start_chapter=26,
        end_chapter=50,
        checkpoint_path=tmp_path / "audit.checkpoint.json",
        request_chunk=request,
        repair_chunk=repair,
        correct_semantic_chunk=semantic,
        config=config or AuditChunkConfig(max_candidates=2, max_serialized_chars=100_000),
        context_fingerprint=context,
        supersede_incompatible_policy=supersede,
    )


def contextual_normalization_fixture(*, approved_renderings=[]):
    candidates = [
        {
            "candidate_id": "c01",
            "chinese": "甲",
            "english": "Alpha",
        },
        {
            "candidate_id": "c02",
            "chinese": "重合效應",
            "english": "Convergence Effect",
        },
        {
            "candidate_id": "c03",
            "chinese": "乙",
            "english": "Beta",
        },
    ]
    target = {
        "candidate_id": "c02",
        "action": "contextual",
        "english": "Convergence Effect",
        "merge_with": "",
        "notes": "original note",
    }
    if approved_renderings is not None:
        target["approved_renderings"] = copy.deepcopy(approved_renderings)
    original = {
        "decisions": [
            {
                "candidate_id": "c03",
                "action": "keep",
                "english": "Beta",
                "approved_renderings": [],
                "merge_with": "",
                "notes": "third",
            },
            target,
            {
                "candidate_id": "c01",
                "action": "keep",
                "english": "Alpha",
                "approved_renderings": [],
                "merge_with": "",
                "notes": "first",
            },
        ],
        "issues": ["preserve this issue"],
    }
    error = {
        "code": "missing_contextual_renderings",
        "message": "contextual decision requires approved_renderings",
        "retryable": True,
        "candidate_id": "c02",
    }
    return candidates, original, [error]


def canonical_result_hash(value):
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def evaluate_runtime_audit_prompt(
    production_tree,
    production_path,
    *,
    profile,
    novel_policy,
    profile_audit_instructions,
):
    initializer = next(
        node
        for node in production_tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "initialize_production_runtime_state"
    )
    assignments = [
        node.value
        for node in ast.walk(initializer)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "AUDIT_PROMPT"
            for target in node.targets
        )
    ]
    assert len(assignments) == 1

    audit_prompt_base_node = next(
        node.value
        for node in production_tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "AUDIT_PROMPT_BASE"
            for target in node.targets
        )
    )
    audit_prompt_base = ast.literal_eval(audit_prompt_base_node)

    return eval(
        compile(ast.Expression(assignments[0]), str(production_path), "eval"),
        {
            "AUDIT_PROMPT_BASE": audit_prompt_base,
            "profile_audit_instructions": profile_audit_instructions,
            "ACTIVE_PROFILE": profile,
            "ACTIVE_NOVEL_POLICY": novel_policy,
        },
    )


def test_runtime_audit_prompt_reconstruction_uses_initializer_assignment():
    repo = Path(__file__).resolve().parents[1]
    production_path = (
        repo / "translate_production_unattended_scheduled_retroactive_dynamic_pricing.py"
    )
    production_tree = ast.parse(production_path.read_text(encoding="utf-8"))

    from novel_project import load_novel_config
    from translation_profile import (
        load_novel_terminology_policy,
        load_translation_profile,
        profile_audit_instructions,
    )

    base = repo / "novels/guhuo_bird"
    novel_config = load_novel_config(base / "novel.json")
    profile = load_translation_profile(novel_config.profile, repo)
    novel_policy = load_novel_terminology_policy(
        profile, base / "translation_addendum.txt"
    )

    audit_prompt = evaluate_runtime_audit_prompt(
        production_tree,
        production_path,
        profile=profile,
        novel_policy=novel_policy,
        profile_audit_instructions=profile_audit_instructions,
    )

    assert isinstance(audit_prompt, str)
    assert audit_prompt
    assert f"PROFILE: {profile.display_name} ({profile.id})" in audit_prompt


@pytest.mark.parametrize("approved", [[], None])
def test_contextual_rendering_is_promoted_locally_without_other_changes(approved):
    candidates, original, errors = contextual_normalization_fixture(
        approved_renderings=approved
    )
    before = copy.deepcopy(original)

    normalized, audit = try_deterministic_glossary_semantic_normalization(
        original, errors, candidates
    )

    assert original == before
    assert [item["candidate_id"] for item in normalized["decisions"]] == [
        "c03", "c02", "c01"
    ]
    target = next(
        item for item in normalized["decisions"] if item["candidate_id"] == "c02"
    )
    assert target == {
        **next(item for item in before["decisions"] if item["candidate_id"] == "c02"),
        "approved_renderings": ["Convergence Effect"],
    }
    assert normalized["decisions"][0] == before["decisions"][0]
    assert normalized["decisions"][2] == before["decisions"][2]
    assert normalized["issues"] == before["issues"]
    assert audit == {
        "method": "deterministic_local",
        "policy": "contextual-rendering-local-v1",
        "candidate_id": "c02",
        "validation_code": "missing_contextual_renderings",
        "applied_fields": ["approved_renderings"],
        "source_rendering": "Convergence Effect",
        "api_calls": 0,
        "cost_usd": 0,
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda candidates, original, errors: original["decisions"][1].update(
            approved_renderings=["Convergence Effect"]
        ),
        lambda candidates, original, errors: original["decisions"][1].update(
            english=""
        ),
        lambda candidates, original, errors: original["decisions"][1].update(
            english="Newly Invented Effect"
        ),
        lambda candidates, original, errors: original["decisions"][1].update(
            approved_renderings="Convergence Effect"
        ),
        lambda candidates, original, errors: original["decisions"][1].update(
            approved_renderings=[""]
        ),
        lambda candidates, original, errors: original["decisions"][1].update(
            action="keep"
        ),
        lambda candidates, original, errors: original["decisions"][1].update(
            action="correct"
        ),
        lambda candidates, original, errors: errors[0].update(
            candidate_id="c99"
        ),
        lambda candidates, original, errors: original["decisions"][1].update(
            chinese="不同來源"
        ),
    ],
    ids=[
        "nonempty-valid-list",
        "empty-english",
        "changed-english",
        "non-list-renderings",
        "nonempty-invalid-list",
        "keep-action",
        "correct-action",
        "unknown-candidate",
        "source-identity-mismatch",
    ],
)
def test_contextual_rendering_local_normalization_fails_closed(mutate):
    candidates, original, errors = contextual_normalization_fixture()
    mutate(candidates, original, errors)
    assert try_deterministic_glossary_semantic_normalization(
        original, errors, candidates
    ) is None


def test_contextual_rendering_local_result_passes_full_chunk_validator():
    candidates, original, errors = contextual_normalization_fixture()
    normalized, _audit = try_deterministic_glossary_semantic_normalization(
        original, errors, candidates
    )
    sources = {item["candidate_id"]: item["chinese"] for item in candidates}
    validated = validate_audit_chunk_result(
        normalized,
        ["c01", "c02", "c03"],
        sources.values(),
        sources,
    )
    assert next(
        item for item in validated["decisions"] if item["candidate_id"] == "c02"
    )["approved_renderings"] == ["Convergence Effect"]


def test_missing_contextual_renderings_uses_local_normalization_before_api(tmp_path):
    items = [identified_candidate("重合效應", "Convergence Effect")]
    correction_calls = []

    result = run_audit(
        tmp_path,
        items,
        lambda targets, metadata: valid_identified_response(
            targets,
            stats={},
            overrides={
                "c01": {
                    "action": "contextual",
                    "english": "Convergence Effect",
                    "approved_renderings": [],
                    "notes": "unchanged",
                }
            },
        ),
        semantic=lambda parsed, errors, metadata: correction_calls.append(metadata),
    )

    assert correction_calls == []
    chunk = result["checkpoint"]["chunks"][0]
    assert chunk["result"]["decisions"][0]["action"] == "contextual"
    assert chunk["result"]["decisions"][0]["english"] == "Convergence Effect"
    assert chunk["result"]["decisions"][0]["notes"] == "unchanged"
    assert chunk["result"]["decisions"][0]["approved_renderings"] == [
        "Convergence Effect"
    ]
    assert chunk["deterministic_normalization"]["api_calls"] == 0
    assert chunk["deterministic_normalization"]["cost_usd"] == 0
    assert result["stats"]["api_request_count"] == 1


def historical_failed_contextual_checkpoint(tmp_path, *, current_english):
    items = [
        identified_candidate(f"term-{index:02d}", f"Term {index:02d}")
        for index in range(1, 17)
    ]
    items[1]["english"] = current_english
    config = AuditChunkConfig(max_candidates=20, max_serialized_chars=100_000)
    initial = run_audit(
        tmp_path,
        items,
        lambda targets, metadata: valid_identified_response(targets),
        config=config,
    )
    checkpoint = initial["checkpoint"]
    checkpoint["status"] = "in_progress"
    checkpoint.pop("combined_result", None)
    chunk = checkpoint["chunks"][0]
    chunk["status"] = "failed"
    chunk["attempts"] = 1
    chunk.pop("result", None)
    chunk["last_error"] = "historical semantic correction scope failure"
    chunk["semantic_correction_attempted"] = True
    chunk["semantic_correction_operation_id"] = "saved-correction"

    primary_decisions = []
    for index in range(1, 17):
        primary_decisions.append({
            "candidate_id": f"c{index:02d}",
            "action": "keep",
            "english": f"Term {index:02d}",
            "approved_renderings": [],
            "merge_with": "",
            "notes": f"note {index:02d}",
        })
    primary_decisions[1].update({
        "action": "contextual",
        "english": "Convergence Effect",
        "approved_renderings": [],
        "notes": "Li Yan's informal reference",
    })
    primary = {"decisions": primary_decisions, "issues": ["saved issue"]}
    correction = {"decisions": [{"candidate_id": "c02", "action": "keep"}]}
    checkpoint["attempt_records"] = [
        {
            "chunk_id": "c0001",
            "kind": "audit",
            "attempt": 1,
            "operation_id": "saved-primary",
            "lifecycle_version": "audit-attempt-lifecycle-v1",
            "lifecycle": "validation_failed",
            "model_id_transport_version": "short-request-ids-v1",
            "stats": {"cost_usd": 0.0200453},
            "parsed_result": primary,
            "result_hash": canonical_result_hash(primary),
            "validation_errors": [{
                "code": "missing_contextual_renderings",
                "message": (
                    "contextual decision for term-02 requires approved_renderings"
                ),
                "retryable": True,
                "candidate_id": items[1]["candidate_id"],
            }],
        },
        {
            "chunk_id": "c0001",
            "kind": "semantic_correction",
            "attempt": 1,
            "operation_id": "saved-correction",
            "lifecycle_version": "audit-attempt-lifecycle-v1",
            "lifecycle": "validation_failed",
            "model_id_transport_version": "short-request-ids-v1",
            "base_result_hash": canonical_result_hash(primary),
            "stats": {"cost_usd": 0.00593538},
            "parsed_result": correction,
            "result_hash": canonical_result_hash(correction),
            "validation_errors": [{
                "code": "semantic_correction_scope_violation",
                "message": (
                    "semantic correction modified fields outside its authorized "
                    "scope for c02"
                ),
                "retryable": False,
                "candidate_id": "c02",
            }],
        },
    ]
    (tmp_path / "audit.checkpoint.json").write_text(
        json.dumps(checkpoint), encoding="utf-8"
    )
    return items, config, copy.deepcopy(checkpoint["attempt_records"]), primary


def test_historical_failed_action_change_is_ignored_and_primary_recovers(tmp_path):
    items, config, historical_records, primary = historical_failed_contextual_checkpoint(
        tmp_path, current_english="Convergence Effect"
    )
    audit_calls = []
    correction_calls = []
    result = run_audit(
        tmp_path,
        items,
        lambda targets, metadata: audit_calls.append(metadata),
        semantic=lambda parsed, errors, metadata: correction_calls.append(metadata),
        config=config,
    )

    assert audit_calls == correction_calls == []
    checkpoint = result["checkpoint"]
    assert checkpoint["attempt_records"] == historical_records
    assert len(checkpoint["attempt_records"]) == 2
    decision = checkpoint["chunks"][0]["result"]["decisions"][1]
    assert decision["action"] == "contextual"
    assert decision["english"] == "Convergence Effect"
    assert decision["notes"] == "Li Yan's informal reference"
    assert decision["approved_renderings"] == ["Convergence Effect"]
    assert checkpoint["chunks"][0]["result"]["issues"] == primary["issues"]
    assert checkpoint["chunks"][0]["deterministic_normalization"]["api_calls"] == 0
    assert result["stats"]["api_request_count"] == 2


def test_unsafe_historical_failed_correction_is_not_reissued(tmp_path):
    items, config, historical_records, _primary = historical_failed_contextual_checkpoint(
        tmp_path, current_english="Current Different Rendering"
    )
    audit_calls = []
    correction_calls = []

    with pytest.raises(AuditChunkFailure, match="outside its authorized scope"):
        run_audit(
            tmp_path,
            items,
            lambda targets, metadata: audit_calls.append(metadata),
            semantic=lambda parsed, errors, metadata: correction_calls.append(metadata),
            config=config,
        )

    assert audit_calls == correction_calls == []
    checkpoint = json.loads(
        (tmp_path / "audit.checkpoint.json").read_text(encoding="utf-8")
    )
    assert checkpoint["attempt_records"] == historical_records
    assert len(checkpoint["attempt_records"]) == 2


def test_missing_contextual_authorization_and_sparse_patch_contract():
    _candidates, original, errors = contextual_normalization_fixture()
    assert audit_semantic_correction_authorization(errors) == {
        "c02": {"approved_renderings"}
    }
    with pytest.raises(AuditResultValidationError):
        audit_module.merge_audit_semantic_correction(
            original,
            {"decisions": [{"candidate_id": "c02", "action": "keep"}]},
            errors,
        )
    with pytest.raises(AuditResultValidationError, match="no effective"):
        audit_module.merge_audit_semantic_correction(
            original,
            {"decisions": [{"candidate_id": "c02", "approved_renderings": []}]},
            errors,
        )
    effective, merge_audit = audit_module.merge_audit_semantic_correction(
        original,
        {
            "decisions": [{
                "candidate_id": "c02",
                "approved_renderings": ["Convergence Effect"],
            }]
        },
        errors,
    )
    assert effective["decisions"][1]["action"] == "contextual"
    assert effective["decisions"][1]["approved_renderings"] == [
        "Convergence Effect"
    ]
    assert merge_audit["authorization"] == {"c02": ["approved_renderings"]}


def test_candidate_list_below_threshold_is_one_chunk():
    chunks = build_audit_chunks(
        [candidate("a"), candidate("b")],
        AuditChunkConfig(max_candidates=3, max_serialized_chars=100_000),
    )
    assert [chunk["candidate_ids"] for chunk in chunks] == [["a", "b"]]


def test_candidate_list_above_threshold_has_deterministic_chunks():
    items = [candidate(letter) for letter in "fedcba"]
    config = AuditChunkConfig(max_candidates=2, max_serialized_chars=100_000)
    first = build_audit_chunks(items, config)
    second = build_audit_chunks(list(reversed(items)), config)
    expected = [["a", "b"], ["c", "d"], ["e", "f"]]
    assert [chunk["candidate_ids"] for chunk in first] == expected
    assert [chunk["candidate_ids"] for chunk in second] == expected


def test_serialized_size_bound_can_split_below_count_limit():
    items = [candidate("a", "A" * 80), candidate("b", "B" * 80)]
    chunks = build_audit_chunks(
        items,
        AuditChunkConfig(max_candidates=10, max_serialized_chars=240),
    )
    assert [chunk["candidate_ids"] for chunk in chunks] == [["a"], ["b"]]


def test_known_alias_and_shared_target_families_stay_together_when_possible():
    items = [
        candidate("variant", "Canonical", alias_of="root"),
        candidate("other", "Other"),
        candidate("root", "Canonical"),
    ]
    chunks = build_audit_chunks(
        items,
        AuditChunkConfig(max_candidates=2, max_serialized_chars=100_000),
    )
    assert any(chunk["candidate_ids"] == ["root", "variant"] for chunk in chunks)


def test_all_valid_chunks_are_combined_once(tmp_path):
    items = [candidate(letter) for letter in "abc"]
    calls = []

    def request(targets, metadata):
        calls.append(metadata["chunk_id"])
        return valid_response(targets)

    result = run_audit(tmp_path, items, request)
    assert calls == ["c0001", "c0002"]
    assert [item["chinese"] for item in result["audit_result"]["decisions"]] == [
        "a",
        "b",
        "c",
    ]
    assert result["stats"]["api_request_count"] == 2
    checkpoint = json.loads((tmp_path / "audit.checkpoint.json").read_text(encoding="utf-8"))
    assert checkpoint["status"] == "completed"


def test_malformed_second_chunk_preserves_first_checkpoint_and_no_live_mutation(tmp_path):
    items = [candidate(letter) for letter in "abc"]
    live_glossary = {item["chinese"]: item["english"] for item in items}
    before = dict(live_glossary)

    def request(targets, metadata):
        if metadata["chunk_id"] == "c0002":
            return {"content": '{"decisions":[', "finish_reason": "stop", "stats": {}}
        return valid_response(targets)

    def bad_repair(content, metadata):
        return {"content": "still broken", "finish_reason": "stop", "stats": {}}

    with pytest.raises(AuditChunkFailure):
        run_audit(tmp_path, items, request, repair=bad_repair)
    checkpoint = json.loads((tmp_path / "audit.checkpoint.json").read_text(encoding="utf-8"))
    assert checkpoint["chunks"][0]["status"] == "completed"
    assert checkpoint["chunks"][1]["status"] == "failed"
    assert live_glossary == before


def test_restart_reuses_recorded_failed_json_repair_without_repeating_requests(tmp_path):
    items = [candidate(letter) for letter in "abc"]

    def first_request(targets, metadata):
        if metadata["chunk_id"] == "c0002":
            return {"content": "broken", "finish_reason": "stop", "stats": {}}
        return valid_response(targets)

    with pytest.raises(AuditChunkFailure):
        run_audit(
            tmp_path,
            items,
            first_request,
            repair=lambda content, metadata: {
                "content": "broken",
                "finish_reason": "stop",
                "stats": {},
            },
        )

    resumed_calls = []

    def resumed_request(targets, metadata):
        resumed_calls.append(metadata["chunk_id"])
        return valid_response(targets)

    with pytest.raises(AuditChunkFailure, match="JSON repair failed validation"):
        run_audit(tmp_path, items, resumed_request)
    assert resumed_calls == []


def test_nontruncated_malformed_json_uses_only_chunk_repair(tmp_path):
    items = [candidate("a")]
    repairs = []

    def request(targets, metadata):
        return {"content": '{"decisions":', "finish_reason": "stop", "stats": {}}

    def repair(content, metadata):
        repairs.append((content, metadata["chunk_id"]))
        return valid_response(items)

    result = run_audit(tmp_path, items, request, repair=repair)
    assert repairs == [('{"decisions":', "c0001")]
    assert result["stats"]["api_request_count"] == 2


def test_known_truncation_skips_json_repair_and_adaptively_subdivides(tmp_path):
    items = [candidate(letter) for letter in "abcd"]
    calls = []
    repairs = []

    def request(targets, metadata):
        calls.append((metadata["chunk_id"], len(targets)))
        if metadata["chunk_id"] == "c0001":
            return {"content": '{"decisions":[', "finish_reason": "length", "stats": {}}
        return valid_response(targets)

    config = AuditChunkConfig(max_candidates=4, max_serialized_chars=100_000)
    result = run_audit(
        tmp_path,
        items,
        request,
        repair=lambda content, metadata: repairs.append(metadata) or {},
        config=config,
    )
    assert calls == [("c0001", 4), ("c0001.1", 2), ("c0001.2", 2)]
    assert repairs == []
    assert len(result["audit_result"]["decisions"]) == 4


def test_repeated_minimum_chunk_truncation_stops_without_infinite_retry(tmp_path):
    items = [candidate("a")]
    calls = []

    def truncated(targets, metadata):
        calls.append(metadata["chunk_id"])
        return {"content": "{", "finish_reason": "length", "stats": {}}

    with pytest.raises(AuditChunkFailure):
        run_audit(tmp_path, items, truncated)
    with pytest.raises(AuditChunkFailure):
        run_audit(tmp_path, items, truncated)
    with pytest.raises(AuditChunkFailure):
        run_audit(tmp_path, items, truncated)
    assert calls == ["c0001", "c0001"]


def test_completed_checkpoint_restart_makes_no_requests(tmp_path):
    items = [candidate("a")]
    run_audit(tmp_path, items, lambda targets, metadata: valid_response(targets))

    def forbidden_request(targets, metadata):
        raise AssertionError("completed audit must not run again")

    result = run_audit(tmp_path, items, forbidden_request)
    assert result["resumed"] is True
    assert result["audit_result"]["decisions"][0]["chinese"] == "a"


def test_candidate_or_configuration_hash_mismatch_rejects_stale_checkpoint(tmp_path):
    items = [candidate("a")]
    run_audit(tmp_path, items, lambda targets, metadata: valid_response(targets))
    changed = [candidate("a", "Changed")]
    with pytest.raises(AuditCheckpointMismatch, match="candidate set/configuration hash"):
        run_audit(tmp_path, changed, lambda targets, metadata: valid_response(targets))


def test_conflicting_cross_chunk_merge_decisions_block_final_result(tmp_path):
    items = [candidate("a", "Alpha"), candidate("b", "Beta")]

    def request(targets, metadata):
        source = targets[0]["chinese"]
        target = "b" if source == "a" else "a"
        return valid_response(
            targets,
            overrides={source: {"action": "merge", "merge_with": target}},
        )

    config = AuditChunkConfig(max_candidates=1, max_serialized_chars=100_000)
    with pytest.raises(ValueError, match="merge cycle"):
        run_audit(tmp_path, items, request, config=config)


def test_schema_validation_rejects_omitted_candidate_and_preserves_completed_work(tmp_path):
    items = [candidate("a"), candidate("b")]

    def request(targets, metadata):
        payload = {"decisions": [], "issues": []}
        return {"content": json.dumps(payload), "finish_reason": "stop", "stats": {}}

    with pytest.raises(AuditChunkFailure, match="omitted candidate"):
        run_audit(tmp_path, items, request)
    checkpoint = json.loads((tmp_path / "audit.checkpoint.json").read_text(encoding="utf-8"))
    assert checkpoint["status"] == "in_progress"
    assert all(chunk["status"] != "completed" for chunk in checkpoint["chunks"])


def test_in_flight_chunk_with_recorded_response_is_validated_without_request(tmp_path):
    items = [candidate("a")]
    config = AuditChunkConfig(max_candidates=2, max_serialized_chars=100_000)
    checkpoint_path = tmp_path / "audit.checkpoint.json"
    first = run_audit(tmp_path, items, lambda targets, metadata: valid_response(targets), config=config)
    checkpoint = first["checkpoint"]
    checkpoint["status"] = "in_progress"
    checkpoint["chunks"][0]["status"] = "in_flight"
    checkpoint.pop("combined_result", None)
    checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
    calls = []
    run_audit(
        tmp_path,
        items,
        lambda targets, metadata: calls.append(metadata["chunk_id"]) or valid_response(targets),
        config=config,
    )
    assert calls == []


def test_legacy_small_audit_result_shape_remains_compatible(tmp_path):
    items = [candidate("a")]
    result = run_audit(tmp_path, items, lambda targets, metadata: valid_response(targets))
    assert set(result["audit_result"]) == {"decisions", "issues"}
    assert result["audit_result"]["issues"] == []


def test_explicit_candidate_id_is_accepted_and_source_key_is_reattached(tmp_path):
    items = [identified_candidate("마나 심장")]
    result = run_audit(
        tmp_path,
        items,
        lambda targets, metadata: valid_identified_response(targets),
    )
    decision = result["audit_result"]["decisions"][0]
    assert decision["candidate_id"] == items[0]["candidate_id"]
    assert decision["chinese"] == "마나 심장"


def test_explicit_audit_uses_short_ids_but_checkpoints_internal_ids(tmp_path):
    items = [identified_candidate("alpha"), identified_candidate("beta")]
    observed = {}

    def request(targets, metadata):
        observed["target_ids"] = [item["candidate_id"] for item in targets]
        observed["metadata"] = metadata
        return valid_identified_response(targets)

    result = run_audit(tmp_path, items, request)
    internal_ids = [item["candidate_id"] for item in items]
    assert observed["target_ids"] == ["c01", "c02"]
    assert observed["metadata"]["model_candidate_ids"] == ["c01", "c02"]
    assert observed["metadata"]["candidate_ids"] == internal_ids
    checkpoint = result["checkpoint"]
    assert checkpoint["chunks"][0]["candidate_ids"] == internal_ids
    assert [
        decision["candidate_id"]
        for decision in checkpoint["chunks"][0]["result"]["decisions"]
    ] == internal_ids
    assert checkpoint["attempt_records"][0]["model_id_transport_version"] == (
        "short-request-ids-v1"
    )


def test_semantic_correction_reuses_short_ids_and_resolves_internal_identity(
    tmp_path,
):
    items = [identified_candidate("alpha")]
    observed = {}

    def request(targets, metadata):
        return valid_identified_response(
            targets, overrides={"c01": {"action": ""}}
        )

    def correct(parsed, errors, metadata):
        observed["parsed_id"] = parsed["decisions"][0]["candidate_id"]
        observed["error_id"] = errors[0]["candidate_id"]
        observed["source_ids"] = list(metadata["candidate_sources"])
        return response_for_parsed(
            {"decisions": [{"candidate_id": "c01", "action": "keep"}]}
        )

    result = run_audit(tmp_path, items, request, semantic=correct)
    assert observed == {
        "parsed_id": "c01",
        "error_id": "c01",
        "source_ids": ["c01"],
    }
    assert result["audit_result"]["decisions"][0]["candidate_id"] == (
        items[0]["candidate_id"]
    )


def test_semantic_correction_cannot_change_short_candidate_identity(tmp_path):
    items = [identified_candidate("alpha")]

    def request(targets, metadata):
        return valid_identified_response(
            targets, overrides={"c01": {"action": ""}}
        )

    def corrupt_identity(parsed, errors, metadata):
        return response_for_parsed(
            {"decisions": [{"candidate_id": "c02", "action": "keep"}]}
        )

    with pytest.raises(AuditChunkFailure, match="changed candidate identity coverage"):
        run_audit(tmp_path, items, request, semantic=corrupt_identity)


def test_pretransport_completed_internal_chunk_remains_reusable(tmp_path):
    items = [identified_candidate("alpha"), identified_candidate("beta")]
    run_audit(
        tmp_path,
        items,
        lambda targets, metadata: valid_identified_response(targets),
    )
    checkpoint_path = tmp_path / "audit.checkpoint.json"
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    for record in checkpoint["attempt_records"]:
        record.pop("model_id_transport_version", None)
    checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
    calls = []

    result = run_audit(
        tmp_path,
        items,
        lambda targets, metadata: calls.append(metadata) or None,
    )

    assert calls == []
    assert result["resumed"] is True
    assert [
        decision["candidate_id"] for decision in result["audit_result"]["decisions"]
    ] == [item["candidate_id"] for item in items]


def test_partial_internal_checkpoint_reuses_completed_and_recorded_failed_response(
    tmp_path,
):
    items = [identified_candidate("alpha"), identified_candidate("beta")]
    config = AuditChunkConfig(max_candidates=1, max_serialized_chars=100_000)

    def first_run(targets, metadata):
        if metadata["chunk_id"] == "c0001":
            return valid_identified_response(targets)
        return response_for_parsed(
            {
                "decisions": [
                    {
                        "candidate_id": metadata["candidate_ids"][0],
                        "action": "keep",
                    }
                ],
                "issues": [],
            }
        )

    with pytest.raises(AuditChunkFailure, match="outside this request"):
        run_audit(tmp_path, items, first_run, config=config)
    checkpoint_path = tmp_path / "audit.checkpoint.json"
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    checkpoint["attempt_records"][0].pop("model_id_transport_version", None)
    checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
    calls = []

    def resumed(targets, metadata):
        calls.append(metadata["chunk_id"])
        return valid_identified_response(targets)

    with pytest.raises(AuditChunkFailure, match="outside this request"):
        run_audit(tmp_path, items, resumed, config=config)
    assert calls == []


def test_semantic_correction_operation_id_is_stable_with_short_transport(tmp_path):
    items = [identified_candidate("alpha")]
    operation_ids = []

    def request(targets, metadata):
        return valid_identified_response(
            targets, overrides={"c01": {"action": ""}}
        )

    def correct(parsed, errors, metadata):
        operation_ids.append(metadata["operation_id"])
        return response_for_parsed(
            {"decisions": [{"candidate_id": "c01", "action": "keep"}]}
        )

    run_audit(tmp_path / "first", items, request, semantic=correct)
    run_audit(tmp_path / "second", items, request, semantic=correct)
    assert operation_ids[0] == operation_ids[1]


@pytest.mark.parametrize(
    "override, expected",
    [
        ({"candidate_id": "source:injected"}, "outside this request"),
        ({"chinese": "魔力心脏"}, "rewrote source identity"),
    ],
)
def test_identity_mutation_is_a_hard_stop_without_semantic_retry(
    tmp_path, override, expected
):
    items = [identified_candidate("마나 심장")]
    corrections = []

    def request(targets, metadata):
        return valid_identified_response(
            targets,
            overrides={targets[0]["candidate_id"]: override},
        )

    with pytest.raises(AuditChunkFailure, match=expected):
        run_audit(
            tmp_path,
            items,
            request,
            semantic=lambda parsed, errors, metadata: corrections.append(metadata),
        )
    assert corrections == []


def test_empty_action_gets_one_bounded_semantic_correction(tmp_path):
    items = [identified_candidate("마나 심장")]
    corrections = []

    def request(targets, metadata):
        return valid_identified_response(
            targets,
            overrides={targets[0]["candidate_id"]: {"action": ""}},
        )

    def correct(parsed, errors, metadata):
        corrections.append((parsed, errors, metadata))
        return response_for_parsed(
            {"decisions": [{"candidate_id": "c01", "action": "keep"}]},
            stats={"cost_usd": 0.002},
        )

    result = run_audit(tmp_path, items, request, semantic=correct)
    assert len(corrections) == 1
    assert corrections[0][1][0]["code"] == "invalid_action"
    assert corrections[0][1][0]["retryable"] is True
    assert set(corrections[0][2]["candidate_sources"].values()) == {"마나 심장"}
    assert result["stats"]["api_request_count"] == 2
    record = result["checkpoint"]["attempt_records"][1]
    assert record["kind"] == "semantic_correction"
    assert record["operation_id"].endswith(":semantic-fields-v1")


def test_later_injected_id_cannot_hide_behind_earlier_retryable_field(tmp_path):
    items = [identified_candidate("alpha"), identified_candidate("beta")]
    corrections = []

    def request(targets, metadata):
        payload = {
            "decisions": [
                {
                    "candidate_id": targets[0]["candidate_id"],
                    "action": "",
                    "notes": "",
                },
                {
                    "candidate_id": "source:injected",
                    "action": "keep",
                    "notes": "",
                },
            ],
            "issues": [],
        }
        return response_for_parsed(payload)

    with pytest.raises(AuditChunkFailure, match="outside this request"):
        run_audit(
            tmp_path,
            items,
            request,
            semantic=lambda parsed, errors, metadata: corrections.append(metadata),
        )

    assert corrections == []


def test_semantic_correction_cannot_revise_an_already_valid_decision(tmp_path):
    items = [identified_candidate("alpha"), identified_candidate("beta")]

    def initial(targets, metadata):
        return valid_identified_response(
            targets,
            overrides={targets[0]["candidate_id"]: {"action": ""}},
        )

    def unsafe_correction(parsed, errors, metadata):
        return response_for_parsed({
            "decisions": [
                {"candidate_id": "c01", "action": "keep"},
                {"candidate_id": "c02", "english": "Changed Term"},
            ]
        })

    with pytest.raises(AuditChunkFailure, match="changed candidate identity coverage"):
        run_audit(tmp_path, items, initial, semantic=unsafe_correction)


def test_second_invalid_semantic_response_stops_and_restart_does_not_repeat_it(
    tmp_path,
):
    items = [identified_candidate("마나 심장")]
    corrections = []

    def invalid(targets, metadata):
        return valid_identified_response(
            targets,
            overrides={targets[0]["candidate_id"]: {"action": ""}},
        )

    def bad_correction(parsed, errors, metadata):
        corrections.append(metadata["operation_id"])
        return response_for_parsed(
            {"decisions": [{"candidate_id": "c01", "action": ""}]}
        )

    with pytest.raises(AuditChunkFailure, match="semantic correction failed"):
        run_audit(tmp_path, items, invalid, semantic=bad_correction)
    with pytest.raises(AuditChunkFailure, match="semantic correction failed"):
        run_audit(tmp_path, items, invalid, semantic=bad_correction)
    assert len(corrections) == 1
    checkpoint = json.loads(
        (tmp_path / "audit.checkpoint.json").read_text(encoding="utf-8")
    )
    assert checkpoint["chunks"][0]["semantic_correction_attempted"] is True
    assert sum(
        record["kind"] == "semantic_correction"
        for record in checkpoint["attempt_records"]
    ) == 1


def test_omitted_candidate_does_not_receive_semantic_retry(tmp_path):
    items = [identified_candidate("a"), identified_candidate("b")]
    corrections = []

    def request(targets, metadata):
        payload = {
            "decisions": [
                {
                    "candidate_id": targets[0]["candidate_id"],
                    "action": "keep",
                    "notes": "",
                }
            ],
            "issues": [],
        }
        return response_for_parsed(payload)

    with pytest.raises(AuditChunkFailure, match="omitted request-local candidate"):
        run_audit(
            tmp_path,
            items,
            request,
            semantic=lambda parsed, errors, metadata: corrections.append(metadata),
        )
    assert corrections == []


def test_candidate_set_hash_changes_when_integrity_filter_changes_membership():
    config = AuditChunkConfig(max_candidates=20, max_serialized_chars=100_000)
    backed = [identified_candidate("backed")]
    polluted = backed + [identified_candidate("unbacked")]
    assert audit_candidate_set_hash(backed, config, "context") != (
        audit_candidate_set_hash(polluted, config, "context")
    )


def test_policy_upgrade_archives_failed_checkpoint_and_starts_fresh(tmp_path):
    items = [candidate("a")]
    old_config = AuditChunkConfig(
        max_candidates=2,
        max_serialized_chars=100_000,
        policy_version="candidate-chunks-v1",
    )
    with pytest.raises(AuditChunkFailure):
        run_audit(
            tmp_path,
            items,
            lambda targets, metadata: {
                "content": json.dumps(
                    {"decisions": [{"chinese": "a", "action": ""}], "issues": []}
                ),
                "finish_reason": "stop",
                "stats": {"cost_usd": 0.017},
            },
            config=old_config,
        )

    new_config = AuditChunkConfig(
        max_candidates=2,
        max_serialized_chars=100_000,
        policy_version="source-backed-candidate-ids-v2",
    )
    result = run_audit(
        tmp_path,
        items,
        lambda targets, metadata: valid_response(targets),
        config=new_config,
        supersede=True,
    )
    archive = Path(result["checkpoint"]["superseded_checkpoint"])
    archived = json.loads(archive.read_text(encoding="utf-8"))
    assert archived["policy_version"] == "candidate-chunks-v1"
    assert archived["attempt_records"][0]["stats"]["cost_usd"] == 0.017
    assert result["checkpoint"]["policy_version"] == (
        "source-backed-candidate-ids-v2"
    )
    assert result["stats"]["api_request_count"] == 2
    assert result["stats"]["cost_usd"] == pytest.approx(0.027)


def test_production_applies_only_after_chunked_audit_returns_complete():
    source = (
        Path(__file__).resolve().parents[1]
        / "translate_production_unattended_scheduled_retroactive_dynamic_pricing.py"
    ).read_text(encoding="utf-8")
    run_block = source[source.index("def run_periodic_audit("):source.index("def describe_api_error(")]
    assert run_block.index("call_periodic_audit(") < run_block.index("result = finalize_periodic_audit(")
    finalizer = (Path(__file__).resolve().parents[1] / "production_periodic_audit_finalization.py").read_text(encoding="utf-8")
    assert finalizer.count("module.apply_periodic_audit(") == 1
    assert finalizer.index("module.add_accounting_totals_once(candidate") < finalizer.index("proof = transaction_proof(")


def test_structured_alias_family_preserves_exact_identity_and_stable_root():
    root_id = "source:aa771f50201a9aa91702b890bd0e66914f283ece8c0efbc7d2dc516aefe9f0b0"
    alias_id = "source:32eae8a749b99704e3b5310621ccd9d73ebd48892e97610bda6f75cd0f3368db"
    root = identified_candidate("阿九", "Ajiu")
    alias = identified_candidate("閻阿九", "Ajiu")
    root["candidate_id"] = root_id
    alias["candidate_id"] = alias_id
    alias["alias_of"] = "阿九"
    entries = {
        "阿九": {"english": "Ajiu"},
        "閻阿九": {"english": "Ajiu", "alias_of": "阿九"},
    }

    first = resolve_audit_candidate_families(
        [alias, root], all_glossary_keys=entries, glossary_entries=entries
    )
    second = resolve_audit_candidate_families(
        [root, alias], all_glossary_keys=entries, glossary_entries=entries
    )
    alias_context = first["members_by_candidate_id"][alias_id]
    root_context = first["members_by_candidate_id"][root_id]

    assert alias_context["source_key"] == "閻阿九"
    assert root_context["source_key"] == "阿九"
    assert alias_context["family_id"] == root_context["family_id"]
    assert alias_context["family_root"] == root_context["family_root"] == "阿九"
    assert alias_context["alias_of"] == "阿九"
    assert {
        key: value["family_id"]
        for key, value in first["members_by_candidate_id"].items()
    } == {
        key: value["family_id"]
        for key, value in second["members_by_candidate_id"].items()
    }
    chunks = build_audit_chunks(
        [alias, root],
        AuditChunkConfig(max_candidates=2, max_serialized_chars=100_000),
        all_glossary_keys=entries,
        glossary_entries=entries,
    )
    assert chunks[0]["candidate_ids"] == [root_id, alias_id]
    assert chunks[0]["family_keys"] == [root_context["family_id"]] * 2
    assert set(chunks[0]["family_keys"]).isdisjoint({"阿九", "閻阿九"})

    valid = {
        "decisions": [
            {"candidate_id": root_id, "action": "keep"},
            {"candidate_id": alias_id, "action": "merge", "merge_with": "阿九"},
        ],
        "issues": [],
    }
    assert validate_audit_chunk_result(
        valid,
        [root_id, alias_id],
        entries,
        {root_id: "阿九", alias_id: "閻阿九"},
    )["decisions"][1]["merge_with"] == "阿九"
    invalid = json.loads(json.dumps(valid))
    invalid["decisions"][1]["merge_with"] = "閻阿九"
    with pytest.raises(audit_module.AuditResultValidationError, match="invalid merge target"):
        validate_audit_chunk_result(
            invalid,
            [root_id, alias_id],
            entries,
            {root_id: "阿九", alias_id: "閻阿九"},
        )


def test_existing_alias_relationship_is_explicit_model_context_and_keep_is_neutral(tmp_path):
    items = [identified_candidate("root", "Name"), identified_candidate("alias", "Name")]
    items[1]["alias_of"] = "root"
    entries = {"root": {"english": "Name"}, "alias": {"english": "Name", "alias_of": "root"}}
    before = json.loads(json.dumps(entries))
    observed = {}

    def request(targets, metadata):
        observed["targets"] = json.loads(json.dumps(targets))
        observed["families"] = json.loads(json.dumps(metadata["family_context"]))
        return valid_identified_response(targets)

    run_chunked_audit(
        candidates=items,
        all_glossary_keys=entries,
        glossary_entries=entries,
        start_chapter=1,
        end_chapter=25,
        checkpoint_path=tmp_path / "alias.checkpoint.json",
        request_chunk=request,
        config=AuditChunkConfig(max_candidates=2, max_serialized_chars=100_000),
        context_fingerprint="alias-context-v1",
    )

    alias_target = next(item for item in observed["targets"] if item["source_key"] == "alias")
    alias_context = next(item for item in observed["families"] if item["source_key"] == "alias")
    assert alias_target["alias_of"] == "root"
    assert alias_context["family_root"] == "root"
    assert entries == before


def test_three_member_alias_chain_resolves_one_family_root():
    items = [
        candidate("root", "Name"),
        candidate("middle", "Name", alias_of="root"),
        candidate("leaf", "Name", alias_of="middle"),
    ]
    plan = resolve_audit_candidate_families(items, all_glossary_keys={"root", "middle", "leaf"})
    contexts = plan["members_by_candidate_id"]
    assert {item["family_root"] for item in contexts.values()} == {"root"}
    assert len({item["family_id"] for item in contexts.values()}) == 1
    assert [item["source_key"] for item in plan["families"][0]["members"]] == [
        "root", "leaf", "middle"
    ]


def test_alias_family_cycle_missing_target_and_ambiguity_fail_closed():
    with pytest.raises(ValueError, match="cycle"):
        resolve_audit_candidate_families(
            [candidate("a", alias_of="b"), candidate("b", alias_of="a")],
            all_glossary_keys={"a", "b"},
        )
    with pytest.raises(ValueError, match="missing alias target"):
        resolve_audit_candidate_families([candidate("a", alias_of="missing")])
    with pytest.raises(ValueError, match="ambiguous alias family identity"):
        resolve_audit_candidate_families(
            [candidate("a", alias_of="b")],
            all_glossary_keys={"a", "b", "c"},
            glossary_entries={"a": {"alias_of": "c"}, "b": {}, "c": {}},
        )


def test_same_english_does_not_create_an_alias_family():
    items = [candidate("first", "Same"), candidate("second", "Same")]
    plan = resolve_audit_candidate_families(items)
    assert len(plan["families"]) == 2
    assert len({item["family_id"] for item in plan["members_by_candidate_id"].values()}) == 2


def test_valid_response_is_persisted_before_validation(tmp_path, monkeypatch):
    items = [candidate("a")]
    original_validator = audit_module.validate_audit_chunk_result

    def crash_after_persistence(*args, **kwargs):
        raise RuntimeError("simulated crash before validation")

    monkeypatch.setattr(audit_module, "validate_audit_chunk_result", crash_after_persistence)
    paid_response = valid_response(items)
    paid_response["content"] = "```json\n" + paid_response["content"] + "\n```"
    with pytest.raises(RuntimeError, match="simulated crash"):
        run_audit(tmp_path, items, lambda targets, metadata: paid_response)
    checkpoint = json.loads((tmp_path / "audit.checkpoint.json").read_text(encoding="utf-8"))
    record = checkpoint["attempt_records"][0]
    assert record["lifecycle"] == "response_recorded"
    assert record["raw_content"].startswith("```json\n")
    assert record["parsed_result"]["decisions"][0]["chinese"] == "a"
    assert record["stats"]["cost_usd"] == 0.01
    assert record["result_hash"]

    monkeypatch.setattr(audit_module, "validate_audit_chunk_result", original_validator)
    calls = []
    result = run_audit(
        tmp_path,
        items,
        lambda targets, metadata: calls.append(metadata) or valid_response(targets),
    )
    assert calls == []
    assert result["stats"]["api_request_count"] == 1


def test_semantic_invalid_response_and_stats_survive_restart_without_reissue(tmp_path):
    items = [identified_candidate("a")]
    calls = []

    def invalid(targets, metadata):
        calls.append(metadata["chunk_id"])
        return valid_identified_response(
            targets,
            stats={"prompt_tokens": 9, "completion_tokens": 4, "cost_usd": 0.123},
            overrides={"c01": {"action": "merge", "merge_with": "a"}},
        )

    with pytest.raises(AuditChunkFailure, match="invalid merge target"):
        run_audit(tmp_path, items, invalid)
    checkpoint_path = tmp_path / "audit.checkpoint.json"
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    record = checkpoint["attempt_records"][0]
    assert record["lifecycle"] == "validation_failed"
    assert record["parsed_result"]["decisions"][0]["merge_with"] == "a"
    assert record["stats"]["cost_usd"] == 0.123
    assert record["validation_errors"][0]["code"] == "invalid_merge_target"

    with pytest.raises(AuditChunkFailure, match="invalid merge target"):
        run_audit(tmp_path, items, invalid)
    assert calls == ["c0001"]
    assert len(json.loads(checkpoint_path.read_text(encoding="utf-8"))["attempt_records"]) == 1


def test_semantic_correction_response_is_persisted_and_reused_before_validation(
    tmp_path, monkeypatch
):
    items = [identified_candidate("a")]
    original_validator = audit_module.validate_audit_chunk_result
    validation_calls = 0

    def crash_on_corrected(*args, **kwargs):
        nonlocal validation_calls
        validation_calls += 1
        if validation_calls == 2:
            raise RuntimeError("simulated crash after correction persistence")
        return original_validator(*args, **kwargs)

    monkeypatch.setattr(audit_module, "validate_audit_chunk_result", crash_on_corrected)
    with pytest.raises(RuntimeError, match="after correction persistence"):
        run_audit(
            tmp_path,
            items,
            lambda targets, metadata: valid_identified_response(
                targets, overrides={"c01": {"action": ""}}
            ),
            semantic=lambda parsed, errors, metadata: response_for_parsed(
                {"decisions": [{"candidate_id": "c01", "action": "keep"}]},
                stats={"cost_usd": 0.002},
            ),
        )
    checkpoint = json.loads((tmp_path / "audit.checkpoint.json").read_text(encoding="utf-8"))
    correction = checkpoint["attempt_records"][1]
    assert correction["kind"] == "semantic_correction"
    assert correction["lifecycle"] == "response_recorded"
    assert correction["parsed_result"] == {
        "decisions": [{"candidate_id": "c01", "action": "keep"}]
    }

    monkeypatch.setattr(audit_module, "validate_audit_chunk_result", original_validator)
    audit_calls = []
    correction_calls = []
    result = run_audit(
        tmp_path,
        items,
        lambda targets, metadata: audit_calls.append(metadata),
        semantic=lambda parsed, errors, metadata: correction_calls.append(metadata),
    )
    assert audit_calls == correction_calls == []
    assert result["stats"]["api_request_count"] == 2
    assert result["stats"]["cost_usd"] == pytest.approx(0.012)


def test_orphaned_in_flight_attempt_without_response_never_reissues(tmp_path):
    items = [candidate("a")]
    result = run_audit(tmp_path, items, lambda targets, metadata: valid_response(targets))
    checkpoint = result["checkpoint"]
    checkpoint["status"] = "in_progress"
    checkpoint.pop("combined_result", None)
    checkpoint["chunks"][0].pop("result", None)
    checkpoint["chunks"][0]["status"] = "in_flight"
    record = checkpoint["attempt_records"][0]
    for field in ("raw_content", "raw_content_hash", "parsed_result", "result_hash", "stats"):
        record.pop(field, None)
    record["lifecycle"] = "in_flight"
    (tmp_path / "audit.checkpoint.json").write_text(json.dumps(checkpoint), encoding="utf-8")
    calls = []
    with pytest.raises(AuditChunkFailure, match="remote outcome is uncertain"):
        run_audit(
            tmp_path,
            items,
            lambda targets, metadata: calls.append(metadata) or valid_response(targets),
        )
    assert calls == []


def test_historical_failed_chunk_gets_one_fresh_request_without_repeating_completed(
    tmp_path,
):
    items = [candidate(letter) for letter in "abcdefg"]
    config = AuditChunkConfig(max_candidates=1, max_serialized_chars=100_000)
    initial = run_audit(
        tmp_path,
        items,
        lambda targets, metadata: valid_response(targets),
        config=config,
    )
    checkpoint = initial["checkpoint"]
    checkpoint["status"] = "in_progress"
    checkpoint.pop("combined_result", None)
    sixth = checkpoint["chunks"][5]
    sixth["status"] = "failed"
    sixth["attempts"] = 1
    sixth["last_error"] = "historical semantic validation failure"
    sixth.pop("result", None)
    seventh = checkpoint["chunks"][6]
    seventh["status"] = "pending"
    seventh["attempts"] = 0
    seventh.pop("result", None)
    checkpoint["attempt_records"] = checkpoint["attempt_records"][:6]
    historical = checkpoint["attempt_records"][5]
    for field in (
        "raw_content", "raw_content_hash", "parsed_result", "result_hash",
        "validation_errors", "lifecycle", "lifecycle_version",
    ):
        historical.pop(field, None)
    historical["stats"] = {"prompt_tokens": 100, "completion_tokens": 10, "cost_usd": 0.2}
    checkpoint_path = tmp_path / "audit.checkpoint.json"
    checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")

    classification = classify_audit_chunk_recovery(checkpoint, "c0006")
    assert classification["status"] == "historical_parsed_response_unrecoverable"
    assert classification["fresh_request_required"] is True
    calls = []

    def resumed(targets, metadata):
        calls.append(metadata["chunk_id"])
        return valid_response(targets)

    result = run_audit(tmp_path, items, resumed, config=config)
    assert calls == ["c0006", "c0007"]
    assert [chunk["status"] for chunk in result["checkpoint"]["chunks"]] == ["completed"] * 7
    assert [decision["chinese"] for decision in result["audit_result"]["decisions"]] == list("abcdefg")
    assert result["checkpoint"]["chunks"][5]["recovery_classification"]["status"] == (
        "historical_parsed_response_unrecoverable"
    )


def test_periodic_audit_prompt_distinguishes_family_and_merge_identities():
    source = (
        Path(__file__).resolve().parents[1]
        / "translate_production_unattended_scheduled_retroactive_dynamic_pricing.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    assignments = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name)
            and target.id == "AUDIT_MODEL_PROMPT_SUFFIX"
            for target in node.targets
        )
    ]
    assert len(assignments) == 1
    prompt_suffix = ast.literal_eval(assignments[0].value)
    assert "family_id and the family_root field are grouping context, not merge identities" in prompt_suffix
    assert "If an existing alias_of relationship remains correct, choose KEEP" in prompt_suffix
    assert "MUST differ from the\ncandidate's own source_key" in prompt_suffix


def test_missing_contextual_correction_prompt_is_approved_renderings_only():
    source = (
        Path(__file__).resolve().parents[1]
        / "translate_production_unattended_scheduled_retroactive_dynamic_pricing.py"
    ).read_text(encoding="utf-8")
    assert "approved_renderings is the ONLY authorized" in source
    assert "Do NOT return or change action, english, notes, merge_with, category" in source
    assert "approved_renderings must be a non-empty array of non-empty strings" in source
    assert "metadata['authorized_fields']" in source


def test_real_201_225_checkpoint_and_alias_family_are_read_only():
    repo = Path(__file__).resolve().parents[1]
    base = repo / "novels/guhuo_bird"
    checkpoint_path = base / "output/audits/audit_0201_0225.checkpoint.json"
    if not checkpoint_path.exists():
        pytest.skip("live 201-225 checkpoint is not present in this checkout")
    protected = {
        base / "data/progress.json",
        base / "data/glossary.json",
        base / "data/audit_history.json",
        base / "data/issues.json",
    }

    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    before = {path: digest(path) for path in protected}
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    chunks = {item["chunk_id"]: item for item in checkpoint["chunks"]}
    assert [chunks[f"c{index:04d}"]["status"] for index in range(1, 8)] == [
        "completed"
    ] * 7
    assert chunks["c0006"]["attempts"] == 2
    assert isinstance(chunks["c0006"].get("result"), dict)
    assert classify_audit_chunk_recovery(checkpoint, "c0006")["status"] == (
        "completed"
    )

    glossary = json.loads((base / "data/glossary.json").read_text(encoding="utf-8"))
    chapters = json.loads((base / "data/chapters.json").read_text(encoding="utf-8"))
    plan = build_source_integrity_audit_plan(
        glossary["entries"], chapters, 201, 225
    )
    production_path = (
        repo / "translate_production_unattended_scheduled_retroactive_dynamic_pricing.py"
    )
    production_tree = ast.parse(production_path.read_text(encoding="utf-8"))
    compact_index_node = next(
        node
        for node in production_tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "compact_glossary_index"
    )
    from novel_project import load_novel_config
    from translation_profile import (
        load_novel_terminology_policy,
        load_translation_profile,
        profile_audit_instructions,
    )

    novel_config = load_novel_config(base / "novel.json")
    profile = load_translation_profile(novel_config.profile, repo)
    novel_policy = load_novel_terminology_policy(
        profile, base / "translation_addendum.txt"
    )
    audit_prompt = evaluate_runtime_audit_prompt(
        production_tree,
        production_path,
        profile=profile,
        novel_policy=novel_policy,
        profile_audit_instructions=profile_audit_instructions,
    )
    compact_namespace = {
        "glossary": glossary,
        "MAX_AUDIT_GLOSSARY_CHARS": 50_000,
    }
    exec(
        compile(
            ast.Module(body=[compact_index_node], type_ignores=[]),
            str(production_path),
            "exec",
        ),
        compact_namespace,
    )
    glossary_index = compact_namespace["compact_glossary_index"](
        allowed_source_keys=plan["safe_glossary_keys"]
    )
    context_fingerprint = hashlib.sha256(
        (audit_prompt + "\n" + glossary_index).encode("utf-8")
    ).hexdigest()
    recomputed_hash = audit_candidate_set_hash(
        plan["candidates"],
        AuditChunkConfig(max_candidates=20, max_serialized_chars=24_000),
        context_fingerprint,
    )
    assert len(recomputed_hash) == len(checkpoint["candidate_set_hash"]) == 64
    family_plan = resolve_audit_candidate_families(
        plan["candidates"],
        all_glossary_keys=plan["safe_glossary_keys"],
        glossary_entries=glossary["entries"],
    )
    alias_id = "source:32eae8a749b99704e3b5310621ccd9d73ebd48892e97610bda6f75cd0f3368db"
    root_id = "source:aa771f50201a9aa91702b890bd0e66914f283ece8c0efbc7d2dc516aefe9f0b0"
    candidates = {item["candidate_id"]: item for item in plan["candidates"]}
    assert candidates[alias_id]["chinese"] == "閻阿九"
    assert candidates[root_id]["chinese"] == "阿九"
    assert family_plan["members_by_candidate_id"][alias_id]["alias_of"] == "阿九"
    assert family_plan["members_by_candidate_id"][alias_id]["family_root"] == "阿九"
    assert family_plan["members_by_candidate_id"][root_id]["family_root"] == "阿九"
    assert (
        family_plan["members_by_candidate_id"][alias_id]["family_id"]
        == family_plan["members_by_candidate_id"][root_id]["family_id"]
    )
    assert {path: digest(path) for path in protected} == before


def test_real_251_275_c0005_deterministic_recovery_is_read_only():
    repo = Path(__file__).resolve().parents[1]
    base = repo / "novels/guhuo_bird"
    checkpoint_path = base / "output/audits/audit_0251_0275.checkpoint.json"
    protected = {
        base / "data/progress.json",
        base / "data/glossary.json",
        base / "data/audit_history.json",
        base / "data/issues.json",
        checkpoint_path,
    }

    if any(not path.is_file() for path in protected):
        pytest.skip("real Guhuo audit checkpoint is not present")

    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    before = {path: digest(path) for path in protected}
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    chunks = {item["chunk_id"]: item for item in checkpoint["chunks"]}
    assert [chunks[f"c{index:04d}"]["status"] for index in range(1, 5)] == [
        "completed"
    ] * 4
    assert chunks["c0005"]["status"] == "completed"
    assert chunks["c0005"]["semantic_correction_attempted"] is True
    assert chunks["c0005"]["deterministic_normalization"]["method"] == (
        "deterministic_local"
    )
    assert chunks["c0005"]["deterministic_normalization"]["policy"] == (
        "contextual-rendering-local-v1"
    )
    assert chunks["c0005"]["deterministic_normalization"]["api_calls"] == 0

    records = [
        item for item in checkpoint["attempt_records"]
        if item.get("chunk_id") == "c0005"
    ]
    primary = next(item for item in records if item["kind"] == "audit")
    correction = next(
        item for item in records if item["kind"] == "semantic_correction"
    )
    assert primary["result_hash"] == (
        "f7e509f40fcb11e9b975cc747e6ded555f295ec66704e1632b9f0184c501dd41"
    )
    assert correction["result_hash"] == (
        "9397d819f0446dff40832f2c58f6cd1fe9aba71ef94be655df55a19c0af3398b"
    )
    assert canonical_result_hash(primary["parsed_result"]) == primary["result_hash"]
    assert canonical_result_hash(correction["parsed_result"]) == correction["result_hash"]
    assert correction["parsed_result"] == {
        "decisions": [{"candidate_id": "c02", "action": "keep"}]
    }

    glossary = json.loads((base / "data/glossary.json").read_text(encoding="utf-8"))
    chapters = json.loads((base / "data/chapters.json").read_text(encoding="utf-8"))
    plan = build_source_integrity_audit_plan(
        glossary["entries"], chapters, 251, 275
    )
    production_path = (
        repo / "translate_production_unattended_scheduled_retroactive_dynamic_pricing.py"
    )
    production_tree = ast.parse(production_path.read_text(encoding="utf-8"))
    compact_index_node = next(
        node
        for node in production_tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "compact_glossary_index"
    )
    from novel_project import load_novel_config
    from translation_profile import (
        load_novel_terminology_policy,
        load_translation_profile,
        profile_audit_instructions,
    )

    novel_config = load_novel_config(base / "novel.json")
    profile = load_translation_profile(novel_config.profile, repo)
    novel_policy = load_novel_terminology_policy(
        profile, base / "translation_addendum.txt"
    )
    audit_prompt = evaluate_runtime_audit_prompt(
        production_tree,
        production_path,
        profile=profile,
        novel_policy=novel_policy,
        profile_audit_instructions=profile_audit_instructions,
    )
    compact_namespace = {
        "glossary": glossary,
        "MAX_AUDIT_GLOSSARY_CHARS": 50_000,
    }
    exec(
        compile(
            ast.Module(body=[compact_index_node], type_ignores=[]),
            str(production_path),
            "exec",
        ),
        compact_namespace,
    )
    glossary_index = compact_namespace["compact_glossary_index"](
        allowed_source_keys=plan["safe_glossary_keys"]
    )
    context_fingerprint = hashlib.sha256(
        (audit_prompt + "\n" + glossary_index).encode("utf-8")
    ).hexdigest()
    # The live glossary legitimately continues to evolve after this completed
    # audit window, so its newly rebuilt candidate-set hash is not expected to
    # equal the immutable historical checkpoint identity.
    assert len(checkpoint["candidate_set_hash"]) == 64
    assert len(context_fingerprint) == 64
    historical_candidates_by_id = {
        item["candidate_id"]: item
        for item in chunks["c0005"]["result"]["decisions"]
    }
    target_id = (
        "source:fadfaed2cea9c126043a7258dafd01f9d3be588d4c3e77ef03224f63d9add1fc"
    )
    assert chunks["c0005"]["candidate_ids"][1] == target_id
    assert historical_candidates_by_id[target_id]["english"] == (
        "Convergence Effect"
    )

    chunk_candidates = [
        historical_candidates_by_id[candidate_id]
        for candidate_id in chunks["c0005"]["candidate_ids"]
    ]
    id_map = build_model_candidate_id_map(chunks["c0005"]["candidate_ids"])
    request_candidates = model_facing_candidates(chunk_candidates, id_map)
    error = copy.deepcopy(primary["validation_errors"])
    assert error == [{
        "code": "missing_contextual_renderings",
        "message": "contextual decision for 重合效應 requires approved_renderings",
        "retryable": True,
        "candidate_id": target_id,
    }]
    error[0]["candidate_id"] = id_map.internal_to_alias[target_id]
    assert error[0]["candidate_id"] == "c02"
    assert audit_semantic_correction_authorization(error) == {
        "c02": {"approved_renderings"}
    }

    original = primary["parsed_result"]
    original_copy = copy.deepcopy(original)
    normalized, provenance = try_deterministic_glossary_semantic_normalization(
        original, error, request_candidates
    )
    assert original == original_copy
    original_target = original["decisions"][1]
    normalized_target = normalized["decisions"][1]
    assert original_target == {
        "candidate_id": "c02",
        "action": "contextual",
        "english": "Convergence Effect",
        "approved_renderings": [],
        "merge_with": "",
        "notes": (
            "Li Yan's informal reference to the formal '重合現象', not a distinct "
            "system term."
        ),
    }
    assert normalized_target == {
        **original_target,
        "approved_renderings": ["Convergence Effect"],
    }
    assert normalized["decisions"][:1] == original["decisions"][:1]
    assert normalized["decisions"][2:] == original["decisions"][2:]
    assert normalized["issues"] == original["issues"]
    assert provenance["api_calls"] == 0
    assert provenance["cost_usd"] == 0

    with pytest.raises(AuditResultValidationError):
        audit_module.merge_audit_semantic_correction(
            original, correction["parsed_result"], error
        )
    resolved = resolve_model_candidate_response(normalized, id_map)
    validated = validate_audit_chunk_result(
        resolved,
        chunks["c0005"]["candidate_ids"],
        plan["safe_glossary_keys"],
        {
            item["candidate_id"]: item["chinese"]
            for item in chunk_candidates
        },
    )
    validated_target = validated["decisions"][1]
    assert validated_target["action"] == "contextual"
    assert validated_target["english"] == "Convergence Effect"
    assert validated_target["notes"] == original_target["notes"]
    assert validated_target["approved_renderings"] == ["Convergence Effect"]
    assert {path: digest(path) for path in protected} == before
