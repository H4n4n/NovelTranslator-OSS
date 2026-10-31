import copy
import hashlib
import json
from pathlib import Path
import sys
import types
import uuid

import pytest

import glossary_candidate_auto_resolution as auto
from glossary_candidate_auto_resolution import (
    GlossaryCandidateAutoResolutionError,
    build_adjudication_request,
    deterministic_candidate_conflict_resolution,
    resolve_candidate_conflicts_automatically,
    validate_adjudication_result,
)
from glossary_candidate_resolution import (
    AUTOMATIC_RESOLUTION_SCHEMA_VERSION,
    GlossaryCandidateResolutionError,
    apply_candidate_conflict_resolutions,
    candidate_resolution_record_sha256,
    create_candidate_resolution,
    create_automatic_candidate_resolutions,
    load_candidate_resolution_record,
    validate_candidate_resolution_record,
)
from glossary_integrity import (
    GlossaryCandidateIdentityError,
    canonicalize_glossary_review_candidates,
    glossary_candidate_semantic_fingerprint,
    stable_glossary_candidate_id,
)
from novel_project import GlossaryCandidateAutoResolutionConfig, NovelConfig


REPO_ROOT = Path(__file__).resolve().parents[1]
ZERO_STATS = {
    "prompt_tokens": 10,
    "completion_tokens": 4,
    "cache_hit_tokens": 0,
    "cache_miss_tokens": 10,
    "cost_usd": 0.0001,
    "rate_correct_cost_usd": 0.0001,
    "pricing_version": "test-pricing",
    "pricing_period": "off_peak",
    "request_started_utc": "2026-08-30T01:00:00+00:00",
}


def candidate(
    source="å£žè¡€å’’éˆ",
    english="Putrid Blood Curse Spirit",
    category="creature",
    *,
    chapter=209,
    notes="Substantive proposal.",
    context=None,
    raw_position=0,
):
    candidate_id = stable_glossary_candidate_id(source)
    result = {
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
            "span_start": 20 + raw_position,
            "span_end": 24 + raw_position,
        },
    }
    result.update(copy.deepcopy(context or {}))
    return result


def conflict_candidates(
    source="å£žè¡€å’’éˆ",
    *,
    categories=("creature", "monster"),
    english=("Putrid Blood Curse Spirit", "Putrid Blood Curse Spirit"),
    notes=("Substantive proposal.", "Much longer duplicate-like prose is not proof."),
    chapter=209,
    contexts=(None, None),
):
    return [
        candidate(
            source,
            english[index],
            category,
            chapter=chapter,
            notes=notes[index],
            context=contexts[index],
            raw_position=index,
        )
        for index, category in enumerate(categories)
    ]


def conflict_for(candidates):
    with pytest.raises(GlossaryCandidateIdentityError) as caught:
        canonicalize_glossary_review_candidates(candidates)
    return caught.value.diagnostics["conflicts"][0]


def config(*, model=True, normalizations=None):
    return GlossaryCandidateAutoResolutionConfig.from_dict({
        "enabled": True,
        "allow_model_adjudication": model,
        "max_model_requests_per_chapter": 1,
        "category_normalizations": normalizations or {},
    })


def response_for(request, choices=None):
    choices = choices or {}
    decisions = []
    for conflict_id, detail in request["transport"].items():
        option = choices.get(conflict_id)
        if option == "second":
            selected = list(detail["options"])[1]
        elif option is None or option == "first":
            selected = list(detail["options"])[0]
        else:
            selected = option
        decisions.append({
            "conflict_id": conflict_id,
            "selected_proposal_id": selected,
        })
    return json.dumps({"decisions": decisions}), copy.deepcopy(ZERO_STATS)


def run_auto(
    tmp_path,
    conflicts,
    *,
    existing_glossary=None,
    selected=None,
    recovery=None,
    cfg=None,
    request_api=None,
    accounted=None,
    persist_log=None,
    account_log=None,
):
    persist_log = persist_log if persist_log is not None else []
    account_log = account_log if account_log is not None else []
    accounted = accounted if accounted is not None else set()

    def persist(record):
        persist_log.append(copy.deepcopy(record))

    def request(request):
        if request_api is not None:
            return request_api(request)
        return response_for(request, selected)

    def account(operation_id, stats):
        account_log.append((operation_id, copy.deepcopy(stats)))
        accounted.add(operation_id)

    result = resolve_candidate_conflicts_automatically(
        novel_id="synthetic",
        chapter=209,
        pending_identity="1" * 64,
        state_identity="2" * 64,
        conflicts=conflicts,
        source_text="å‰æ–‡å£žè¡€å’’éˆå¾Œæ–‡",
        translation="Before Putrid Blood Curse Spirit after.",
        existing_glossary=existing_glossary or {},
        config=cfg or config(),
        resolution_path=tmp_path / "0209.json",
        recovery_record=recovery,
        persist_recovery=persist,
        request_api=request,
        is_operation_accounted=lambda operation_id: operation_id in accounted,
        account_api_usage=account,
        timestamp_utc="2026-08-30T01:00:00+00:00",
    )
    return result, persist_log, account_log, accounted


def test_deterministic_exact_existing_glossary_identity_is_unique():
    conflict = conflict_for(conflict_candidates())
    result = deterministic_candidate_conflict_resolution(
        [conflict],
        existing_glossary={
            conflict["source_term"]: {
                "english": "Putrid Blood Curse Spirit",
                "category": "monster",
                "status": "provisional",
            }
        },
    )
    assert result["status"] == "resolved"
    assert result["resolved"][0]["conflict"]["source_term"] == conflict["source_term"]
    selected = result["resolved"][0]["selected_semantic_fingerprint"]
    assert selected == next(
        item["semantic_identity_fingerprint"]
        for item in conflict["proposals"]
        if item["category"] == "monster"
    )


def test_deterministic_no_unique_exact_match_stays_unresolved():
    candidates = conflict_candidates(
        categories=("creature", "creature"),
        contexts=({"semantic_scope": "a"}, {"semantic_scope": "b"}),
    )
    conflict = conflict_for(candidates)
    result = deterministic_candidate_conflict_resolution(
        [conflict],
        existing_glossary={
            conflict["source_term"]: {
                "english": "Putrid Blood Curse Spirit",
                "category": "creature",
                "status": "provisional",
            }
        },
    )
    assert result["status"] == "unresolved"


def test_configured_taxonomy_rule_selects_unique_canonical_proposal():
    conflict = conflict_for(conflict_candidates())
    result = deterministic_candidate_conflict_resolution(
        [conflict],
        existing_glossary={},
        category_normalizations={"monster": "creature"},
    )
    assert result["status"] == "resolved"
    assert result["resolved"][0]["basis"][0]["proof"] == (
        "configured_category_normalization"
    )


def test_creature_vs_monster_has_no_implicit_preference():
    conflict = conflict_for(conflict_candidates())
    result = deterministic_candidate_conflict_resolution(
        [conflict], existing_glossary={}
    )
    assert result["status"] == "unresolved"


def test_notes_order_positions_and_fingerprint_list_order_never_rank_proposals():
    original = conflict_for(conflict_candidates())
    reversed_candidates = list(reversed(conflict_candidates()))
    reversed_conflict = conflict_for(reversed_candidates)
    reversed_conflict["conflicting_semantic_fingerprints"].reverse()
    for conflict in (original, reversed_conflict):
        result = deterministic_candidate_conflict_resolution(
            [conflict], existing_glossary={}
        )
        assert result["status"] == "unresolved"


def transport_for_two_conflicts():
    first = conflict_for(conflict_candidates("ç”²"))
    second = conflict_for(conflict_candidates("ä¹™"))
    built = build_adjudication_request(
        chapter=1,
        conflicts=[first, second],
        source_text="ç”²ä¹™",
        translation="Two entities.",
        existing_glossary={},
    )
    return built["request"]["transport"]


def test_model_can_select_first_or_second_existing_proposal():
    transport = transport_for_two_conflicts()
    conflict_id = "c01"
    options = list(transport[conflict_id]["options"])
    for proposal_id in options:
        validated = validate_adjudication_result(
            {"decisions": [
                {"conflict_id": "c01", "selected_proposal_id": proposal_id},
                {
                    "conflict_id": "c02",
                    "selected_proposal_id": list(
                        transport["c02"]["options"]
                    )[0],
                },
            ]},
            transport,
        )
        assert validated["status"] == "resolved"


def test_model_may_explicitly_abstain():
    transport = transport_for_two_conflicts()
    result = validate_adjudication_result(
        {"decisions": [
            {"conflict_id": "c01", "selected_proposal_id": None},
            {
                "conflict_id": "c02",
                "selected_proposal_id": list(transport["c02"]["options"])[0],
            },
        ]},
        transport,
    )
    assert result["status"] == "abstained"


@pytest.mark.parametrize(
    "mutator, code",
    [
        (
            lambda t: {"decisions": [
                {"conflict_id": "c01", "selected_proposal_id": "p99"},
                {"conflict_id": "c02", "selected_proposal_id": None},
            ]},
            "adjudication_unknown_proposal",
        ),
        (
            lambda t: {"decisions": [
                {
                    "conflict_id": "c01",
                    "selected_proposal_id": list(t["c02"]["options"])[0],
                },
                {"conflict_id": "c02", "selected_proposal_id": None},
            ]},
            "adjudication_unknown_proposal",
        ),
        (
            lambda t: {"decisions": [
                {"conflict_id": "c01", "selected_proposal_id": None},
                {"conflict_id": "c01", "selected_proposal_id": None},
                {"conflict_id": "c02", "selected_proposal_id": None},
            ]},
            "adjudication_duplicate_conflict",
        ),
        (
            lambda t: {"decisions": [
                {"conflict_id": "c01", "selected_proposal_id": None}
            ]},
            "adjudication_missing_conflict",
        ),
        (
            lambda t: {"decisions": [
                {
                    "conflict_id": "c01",
                    "selected_proposal_id": None,
                    "english": "Changed",
                },
                {"conflict_id": "c02", "selected_proposal_id": None},
            ]},
            "adjudication_schema_invalid",
        ),
        (
            lambda t: {"decisions": [
                {
                    "conflict_id": "c01",
                    "selected_proposal_id": None,
                    "category": "changed",
                },
                {"conflict_id": "c02", "selected_proposal_id": None},
            ]},
            "adjudication_schema_invalid",
        ),
        (
            lambda t: {
                "decisions": [
                    {"conflict_id": "c01", "selected_proposal_id": None},
                    {"conflict_id": "c02", "selected_proposal_id": None},
                ],
                "new_candidate": {},
            },
            "adjudication_schema_invalid",
        ),
    ],
)
def test_model_mutation_unknown_cross_conflict_duplicate_and_missing_are_rejected(
    mutator, code
):
    transport = transport_for_two_conflicts()
    with pytest.raises(GlossaryCandidateAutoResolutionError) as caught:
        validate_adjudication_result(mutator(transport), transport)
    assert caught.value.code == code


def test_valid_model_selection_creates_v2_without_rewriting_candidates(tmp_path):
    candidates = conflict_candidates(source="\u58de\u8840\u5492\u9748")
    conflict = conflict_for(candidates)
    before = copy.deepcopy(candidates)
    result, persisted, accounts, _ = run_auto(tmp_path, [conflict])
    assert result["status"] == "resolved"
    assert result["method"] == "model_adjudicated"
    assert len(accounts) == 1
    assert candidates == before
    record = load_candidate_resolution_record(tmp_path / "0209.json")
    assert record["schema_version"] == AUTOMATIC_RESOLUTION_SCHEMA_VERSION
    assert record["resolutions"][0]["resolution_method"] == "model_adjudicated"
    assert record["resolutions"][0]["selected_semantic_fingerprint"] == (
        "93f2606e6c66eba372654c4bbdd9252ef584582f9c29c21ec41fd2379c6dbd9a"
    )
    assert persisted[0]["status"] == "initiated"
    assert persisted[1]["status"] == "response_recorded"


def test_model_abstention_stops_without_sidecar_and_consumes_one_call(tmp_path):
    conflict = conflict_for(conflict_candidates())

    def abstain(request):
        conflict_id = next(iter(request["transport"]))
        return json.dumps({"decisions": [{
            "conflict_id": conflict_id,
            "selected_proposal_id": None,
        }]}), copy.deepcopy(ZERO_STATS)

    result, persisted, accounts, _ = run_auto(
        tmp_path, [conflict], request_api=abstain
    )
    assert result["status"] == "unresolved"
    assert result["reason"] == "adjudication_abstained"
    assert len(accounts) == 1
    assert persisted[-1]["status"] == "abstained"
    assert not (tmp_path / "0209.json").exists()


def test_recorded_valid_adjudication_restarts_with_zero_api_and_zero_duplicate_accounting(
    tmp_path, monkeypatch
):
    conflict = conflict_for(conflict_candidates())
    persisted = []
    accounted = set()
    accounts = []
    real_create = auto.create_automatic_candidate_resolutions

    def crash_after_record(*args, **kwargs):
        raise RuntimeError("synthetic crash before sidecar")

    monkeypatch.setattr(auto, "create_automatic_candidate_resolutions", crash_after_record)
    with pytest.raises(RuntimeError):
        run_auto(
            tmp_path,
            [conflict],
            persist_log=persisted,
            accounted=accounted,
            account_log=accounts,
        )
    accepted = persisted[-1]
    assert accepted["status"] == "accepted"
    assert len(accounts) == 1
    monkeypatch.setattr(auto, "create_automatic_candidate_resolutions", real_create)

    def forbidden(_request):
        raise AssertionError("recorded adjudication was reissued")

    second_accounts = []
    result, _, second_accounts, _ = run_auto(
        tmp_path,
        [conflict],
        recovery=accepted,
        request_api=forbidden,
        accounted=accounted,
        account_log=second_accounts,
    )
    assert result["status"] == "resolved"
    assert result["api_called"] is False
    assert second_accounts == []


def test_one_failed_adjudication_never_attempts_again(tmp_path):
    conflict = conflict_for(conflict_candidates())
    calls = []

    def fail(_request):
        calls.append(1)
        raise RuntimeError("synthetic API failure")

    first, persisted, _, accounted = run_auto(
        tmp_path, [conflict], request_api=fail
    )
    assert first["reason"] == "adjudication_api_failed"
    assert len(calls) == 1
    second, _, accounts, _ = run_auto(
        tmp_path,
        [conflict],
        recovery=persisted[-1],
        request_api=lambda request: (_ for _ in ()).throw(
            AssertionError("second request forbidden")
        ),
        accounted=accounted,
    )
    assert second["reason"] == "adjudication_attempt_already_consumed"
    assert accounts == []
    assert len(calls) == 1


def test_invalid_adjudication_is_accounted_once_and_never_retried(tmp_path):
    conflict = conflict_for(conflict_candidates())
    calls = []

    def invalid(_request):
        calls.append(1)
        return json.dumps({"decisions": [{
            "conflict_id": "c01",
            "selected_proposal_id": "p99",
        }]}), copy.deepcopy(ZERO_STATS)

    first, persisted, accounts, accounted = run_auto(
        tmp_path, [conflict], request_api=invalid
    )
    assert first["reason"] == "adjudication_invalid"
    assert len(calls) == 1
    assert len(accounts) == 1
    second_accounts = []
    second, _, second_accounts, _ = run_auto(
        tmp_path,
        [conflict],
        recovery=persisted[-1],
        request_api=lambda request: (_ for _ in ()).throw(
            AssertionError("invalid recorded operation was retried")
        ),
        accounted=accounted,
        account_log=second_accounts,
    )
    assert second["reason"] == "adjudication_invalid"
    assert second_accounts == []
    assert len(calls) == 1


def test_multiple_conflicts_are_batched_into_one_request(tmp_path):
    conflicts = [
        conflict_for(conflict_candidates("ç”²")),
        conflict_for(conflict_candidates("ä¹™")),
    ]
    requests = []

    def request(value):
        requests.append(copy.deepcopy(value))
        return response_for(value)

    result, _, accounts, _ = run_auto(
        tmp_path, conflicts, request_api=request
    )
    assert result["status"] == "resolved"
    assert len(requests) == 1
    assert len(requests[0]["payload"]["conflicts"]) == 2
    assert len(accounts) == 1


def test_oversized_conflict_batch_fails_closed_before_any_request():
    conflicts = [
        conflict_for(conflict_candidates(f"source-{index}"))
        for index in range(auto.MAX_ADJUDICATION_CONFLICTS + 1)
    ]
    with pytest.raises(GlossaryCandidateAutoResolutionError) as caught:
        build_adjudication_request(
            chapter=1,
            conflicts=conflicts,
            source_text="bounded source",
            translation="bounded target",
            existing_glossary={},
        )
    assert caught.value.code == "adjudication_request_too_large"


def test_only_unresolved_conflict_is_sent_after_one_deterministic_resolution(tmp_path):
    deterministic = conflict_for(conflict_candidates("ç”²"))
    model = conflict_for(conflict_candidates("ä¹™"))
    existing = {
        "ç”²": {
            "english": "Putrid Blood Curse Spirit",
            "category": "creature",
            "status": "provisional",
        }
    }
    requests = []

    def request(value):
        requests.append(copy.deepcopy(value))
        return response_for(value)

    result, _, _, _ = run_auto(
        tmp_path,
        [deterministic, model],
        existing_glossary=existing,
        request_api=request,
    )
    assert result["status"] == "resolved"
    assert len(requests) == 1
    assert [item["source"] for item in requests[0]["payload"]["conflicts"]] == [
        "ä¹™"
    ]
    methods = {
        entry["source_key"]: entry["resolution_method"]
        for entry in result["created"]["record"]["resolutions"]
    }
    assert methods == {"ç”²": "deterministic", "ä¹™": "model_adjudicated"}


@pytest.mark.parametrize(
    "change, expected_code",
    [
        ("pending", "auto_resolution_recovery_pending_content_identity_mismatch"),
        ("state", "auto_resolution_recovery_state_extraction_identity_mismatch"),
    ],
)
def test_stale_pending_and_state_identity_reject_recorded_response(
    tmp_path, change, expected_code
):
    conflict = conflict_for(conflict_candidates())

    def abstain(request):
        conflict_id = next(iter(request["transport"]))
        return json.dumps({"decisions": [{
            "conflict_id": conflict_id,
            "selected_proposal_id": None,
        }]}), copy.deepcopy(ZERO_STATS)

    _, persisted, _, _ = run_auto(tmp_path, [conflict], request_api=abstain)
    record = persisted[-1]
    kwargs = {
        "novel_id": "synthetic",
        "chapter": 209,
        "pending_identity": "1" * 64,
        "state_identity": "2" * 64,
        "conflict_fingerprints": [conflict["conflict_fingerprint"]],
    }
    built = build_adjudication_request(
        chapter=209,
        conflicts=[conflict],
        source_text="å‰æ–‡å£žè¡€å’’éˆå¾Œæ–‡",
        translation="Before Putrid Blood Curse Spirit after.",
        existing_glossary={},
    )
    operation_id = record["operation_id"]
    if change == "pending":
        kwargs["pending_identity"] = "3" * 64
    else:
        kwargs["state_identity"] = "4" * 64
    with pytest.raises(GlossaryCandidateAutoResolutionError) as caught:
        auto.validate_adjudication_recovery_record(
            record,
            request_sha256=built["request_sha256"],
            operation_id=operation_id,
            **kwargs,
        )
    assert caught.value.code == expected_code


def test_changed_conflict_or_proposal_set_cannot_reuse_recorded_response(tmp_path):
    conflict = conflict_for(conflict_candidates())

    def abstain(request):
        conflict_id = next(iter(request["transport"]))
        return json.dumps({"decisions": [{
            "conflict_id": conflict_id,
            "selected_proposal_id": None,
        }]}), copy.deepcopy(ZERO_STATS)

    _, persisted, _, _ = run_auto(tmp_path, [conflict], request_api=abstain)
    changed = conflict_for(
        conflict_candidates(categories=("creature", "artifact"))
    )
    with pytest.raises(GlossaryCandidateAutoResolutionError) as caught:
        run_auto(tmp_path, [changed], recovery=persisted[-1])
    assert "mismatch" in caught.value.code


def test_manual_v1_sidecar_wins_before_deterministic_or_model(tmp_path, monkeypatch):
    candidates = conflict_candidates()
    conflict = conflict_for(candidates)
    path = tmp_path / "0209.json"
    selected = glossary_candidate_semantic_fingerprint(candidates[0])
    create_candidate_resolution(
        path,
        novel_id="synthetic",
        chapter=209,
        pending_identity="1" * 64,
        state_identity="2" * 64,
        conflict=conflict,
        selected_semantic_fingerprint=selected,
        rationale="Human approved.",
        timestamp_utc="2026-08-30T00:00:00+00:00",
    )
    monkeypatch.setattr(
        auto,
        "deterministic_candidate_conflict_resolution",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("automatic resolver must not run")
        ),
    )
    result, _, accounts, _ = run_auto(
        tmp_path,
        [conflict],
        request_api=lambda request: (_ for _ in ()).throw(
            AssertionError("model must not run")
        ),
    )
    assert result["status"] == "existing_resolution_present"
    assert accounts == []


def test_v2_is_strict_valid_deterministic_and_cannot_overwrite_human_v1(tmp_path):
    candidates = conflict_candidates()
    conflict = conflict_for(candidates)
    selected = glossary_candidate_semantic_fingerprint(candidates[0])
    auto_path = tmp_path / "auto.json"
    selection = {
        "conflict": conflict,
        "selected_semantic_fingerprint": selected,
        "resolution_method": "deterministic",
        "deterministic_basis": [{
            "proof": "existing_exact_glossary_identity",
            "details": {"source_key": conflict["source_term"]},
        }],
        "adjudication": {},
        "rationale": "Automatically selected exact identity.",
    }
    first = create_automatic_candidate_resolutions(
        auto_path,
        novel_id="synthetic",
        chapter=209,
        pending_identity="1" * 64,
        state_identity="2" * 64,
        selections=[selection],
        timestamp_utc="2026-08-30T00:00:00+00:00",
    )
    before = auto_path.read_bytes()
    second = create_automatic_candidate_resolutions(
        auto_path,
        novel_id="synthetic",
        chapter=209,
        pending_identity="1" * 64,
        state_identity="2" * 64,
        selections=[selection],
        timestamp_utc="2026-08-31T00:00:00+00:00",
    )
    assert second["status"] == "unchanged"
    assert auto_path.read_bytes() == before
    assert first["record"]["record_sha256"] == candidate_resolution_record_sha256(
        first["record"]
    )
    validate_candidate_resolution_record(first["record"])

    human_path = tmp_path / "human.json"
    create_candidate_resolution(
        human_path,
        novel_id="synthetic",
        chapter=209,
        pending_identity="1" * 64,
        state_identity="2" * 64,
        conflict=conflict,
        selected_semantic_fingerprint=selected,
        rationale="Human approved.",
        timestamp_utc="2026-08-30T00:00:00+00:00",
    )
    human_before = human_path.read_bytes()
    with pytest.raises(GlossaryCandidateResolutionError) as caught:
        create_automatic_candidate_resolutions(
            human_path,
            novel_id="synthetic",
            chapter=209,
            pending_identity="1" * 64,
            state_identity="2" * 64,
            selections=[selection],
        )
    assert caught.value.code == "automatic_resolution_human_record_precedence"
    assert human_path.read_bytes() == human_before


def test_stale_v2_fails_closed_and_still_selects_exact_existing_fingerprint(tmp_path):
    conflict = conflict_for(conflict_candidates())
    result, _, _, _ = run_auto(
        tmp_path,
        [conflict],
        cfg=config(model=False, normalizations={"monster": "creature"}),
    )
    record = result["created"]["record"]
    with pytest.raises(GlossaryCandidateResolutionError) as caught:
        load_candidate_resolution_record(
            tmp_path / "0209.json", expected_pending_identity="9" * 64
        )
    assert caught.value.code == "resolution_pending_identity_mismatch"
    candidates = conflict_candidates()
    applied = apply_candidate_conflict_resolutions(
        candidates,
        record,
        novel_id="synthetic",
        chapter=209,
        expected_pending_identity="1" * 64,
        expected_state_identity="2" * 64,
    )
    assert len(applied["candidates"]) == 1
    assert glossary_candidate_semantic_fingerprint(applied["candidates"][0]) == (
        record["resolutions"][0]["selected_semantic_fingerprint"]
    )


def test_config_defaults_on_and_opting_out_turns_off_paid_tier():
    defaults = GlossaryCandidateAutoResolutionConfig.from_dict(None)
    assert defaults.enabled is True
    assert defaults.allow_model_adjudication is True
    opted_out = GlossaryCandidateAutoResolutionConfig.from_dict({"enabled": False})
    assert opted_out.allow_model_adjudication is False
    with pytest.raises(ValueError):
        GlossaryCandidateAutoResolutionConfig.from_dict({
            "enabled": False,
            "allow_model_adjudication": True,
        })
    with pytest.raises(ValueError):
        GlossaryCandidateAutoResolutionConfig.from_dict({
            "enabled": True,
            "allow_model_adjudication": True,
            "max_model_requests_per_chapter": 2,
        })


def test_novel_config_round_trips_auto_resolution_configuration():
    value = {
        "id": "synthetic",
        "title": "Synthetic",
        "source_language": "zh",
        "target_language": "en",
        "profile": "infinite_flow",
        "final_chapter": 10,
        "glossary_candidate_auto_resolution": {
            "enabled": True,
            "allow_model_adjudication": True,
            "max_model_requests_per_chapter": 1,
            "category_normalizations": {"monster": "creature"},
        },
    }
    loaded = NovelConfig.from_dict(value)
    assert loaded.to_dict()["glossary_candidate_auto_resolution"] == value[
        "glossary_candidate_auto_resolution"
    ]


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _load_production_definitions(project_root, monkeypatch):
    monkeypatch.chdir(project_root)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-test-key")
    script = REPO_ROOT / (
        "translate_production_unattended_scheduled_retroactive_dynamic_pricing.py"
    )
    name = f"_candidate_auto_production_{uuid.uuid4().hex}"
    module = types.ModuleType(name)
    module.__file__ = str(script)
    module.__package__ = None
    sys.modules[name] = module
    try:
        source = script.read_text(encoding="utf-8")
        exec(compile(source, str(script), "exec"), module.__dict__)
        module.initialize_production_runtime_state()
    except Exception:
        sys.modules.pop(name, None)
        raise
    return name, module


@pytest.mark.usefixtures("direct_pro_route")
def test_production_catch_runs_one_shot_auto_resolution_then_reuses_sidecar(
    tmp_path, monkeypatch
):
    project = tmp_path / "synthetic_auto"
    source_term = "é‚ªéˆ"
    source_text = f"å‰æ–‡{source_term}å¾Œæ–‡"
    paid_translation = "# Chapter 1\n\n" + (
        "The named curse spirit crossed the chamber without changing form. " * 30
    )
    _write_json(
        project / "novel.json",
        {
            "id": "synthetic_auto",
            "title": "Synthetic Auto",
            "source_language": "zh-Hant",
            "target_language": "en",
            "profile": "infinite_flow",
            "final_chapter": 1,
            "glossary_candidate_auto_resolution": {
                "enabled": True,
                "allow_model_adjudication": True,
                "max_model_requests_per_chapter": 1,
                "category_normalizations": {},
            },
        },
    )
    _write_json(
        project / "data" / "chapters.json",
        [{"chapter": 1, "heading": "ç¬¬ä¸€ç« ", "text": source_text}],
    )
    _write_json(project / "data" / "glossary.json", {"entries": {}})
    _write_json(
        project / "data" / "progress.json",
        {
            "completed": [],
            "failed": [],
            "last_completed": 0,
            "next_chapter": 1,
            "last_audited_chapter": 0,
            "total_prompt_tokens": 0,
            "total_completion_tokens": 0,
            "total_cache_hit_tokens": 0,
            "total_cache_miss_tokens": 0,
            "estimated_cost_usd": 0.0,
            "rate_correct_cost_usd": 0.0,
        },
    )
    _write_json(project / "data" / "continuity.json", {"latest_chapter": 0, "summary": ""})
    _write_json(project / "data" / "issues.json", {"issues": []})
    _write_json(project / "data" / "audit_history.json", {"audits": []})
    pending_md = project / "output" / "pending" / "0001.md"
    pending_json = project / "output" / "pending" / "0001.json"
    pending_md.parent.mkdir(parents=True)
    pending_md.write_text(paid_translation, encoding="utf-8")
    state = {
        "new_terms": [
            {
                "chinese": source_term,
                "english": "Curse Spirit",
                "category": "creature",
                "status": "provisional",
                "notes": "First existing proposal.",
            },
            {
                "chinese": source_term,
                "english": "Curse Spirit",
                "category": "monster",
                "status": "provisional",
                "notes": "Second existing proposal.",
            },
        ],
        "continuity_summary": "Synthetic.",
        "issues": [],
    }
    _write_json(
        pending_json,
        {
            "chapter": 1,
            "stage": "state_extracted",
            "translation_stats": {key: 0 for key in (
                "prompt_tokens", "completion_tokens", "cache_hit_tokens",
                "cache_miss_tokens", "cost_usd", "rate_correct_cost_usd",
            )},
            "elapsed_seconds": 1.0,
            "finish_reason": "stop",
            "state": state,
            "state_stats": {key: 0 for key in (
                "prompt_tokens", "completion_tokens", "cache_hit_tokens",
                "cache_miss_tokens", "cost_usd", "rate_correct_cost_usd",
            )},
        },
    )
    name, production = _load_production_definitions(project, monkeypatch)
    saved_meta = json.loads(pending_json.read_text(encoding="utf-8"))
    saved_meta["state_checkpoint_identity"] = (
        production.current_state_checkpoint_identity(
            1,
            source_text,
            paid_translation,
        )
    )
    _write_json(pending_json, saved_meta)
    calls = []

    def adjudicate(request):
        calls.append(copy.deepcopy(request))
        return response_for(request)

    def stop_before_glossary_review(*args, **kwargs):
        raise production.ChapterGlossaryReviewFailure(
            "synthetic stop after automatic resolution"
        )

    monkeypatch.setattr(
        production, "_auxiliary_request_api", lambda *args: adjudicate
    )
    monkeypatch.setattr(production, "review_new_terms", stop_before_glossary_review)
    monkeypatch.setattr(
        production,
        "translate_chapter",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("paid translation regenerated")
        ),
    )
    monkeypatch.setattr(
        production,
        "extract_state",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("state extraction regenerated")
        ),
    )
    paid_before = pending_md.read_bytes()
    try:
        assert production.process_chapter(1) is False
        assert len(calls) == 1
        sidecar = project / "data" / "glossary_candidate_resolutions" / "0001.json"
        record = load_candidate_resolution_record(sidecar)
        assert record["schema_version"] == AUTOMATIC_RESOLUTION_SCHEMA_VERSION
        first_progress = json.loads(
            (project / "data" / "progress.json").read_text(encoding="utf-8")
        )
        assert len(first_progress["accounted_api_operation_ids"]) == 1
        assert production.process_chapter(1) is False
        assert len(calls) == 1
        second_progress = json.loads(
            (project / "data" / "progress.json").read_text(encoding="utf-8")
        )
        assert second_progress == first_progress
        assert pending_md.read_bytes() == paid_before
    finally:
        sys.modules.pop(name, None)


def _candidates_from_resolution_entry(entry):
    candidates = []
    for proposal in entry["conflicting_proposals"]:
        provenance = copy.deepcopy(proposal["provenance_identity"])
        provenance.update(proposal["source_occurrence_evidence"])
        item = {
            "chinese": entry["source_key"],
            "english": proposal["english"],
            "category": proposal["category"],
            "status": proposal["status"],
            "notes": proposal["notes"],
            "candidate_id": entry["source_concept_id"],
            "source_provenance": provenance,
        }
        item.update(copy.deepcopy(proposal["context_identity"]))
        candidates.append(item)
    return candidates


def test_real_chapter_209_human_sidecar_read_only_precedence_and_hashes():
    sidecar = (
        REPO_ROOT
        / "novels"
        / "guhuo_bird"
        / "data"
        / "glossary_candidate_resolutions"
        / "0209.json"
    )

    chapter = (
        REPO_ROOT
        / "novels"
        / "guhuo_bird"
        / "output"
        / "chapters"
        / "0209.md"
    )

    progress = (
        REPO_ROOT
        / "novels"
        / "guhuo_bird"
        / "data"
        / "progress.json"
    )

    protected = (sidecar, chapter, progress)

    if any(not path.is_file() for path in protected):
        pytest.skip(
            "real Guhuo chapter 209 production fixture is not present"
        )

    before = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in protected
    }

    record = load_candidate_resolution_record(sidecar)
    entry = record["resolutions"][0]
    candidates = _candidates_from_resolution_entry(entry)
    reconstructed = conflict_for(candidates)

    assert reconstructed["conflict_fingerprint"] == (
        "0b828059f72899c6816a208866989fe605ecb6aed3ccaea2cae4d7e6716c94a8"
    )

    api_calls = []
    accounting_calls = []

    applied = apply_candidate_conflict_resolutions(
        candidates,
        record,
        novel_id="guhuo_bird",
        chapter=209,
        expected_pending_identity=record["pending_content_identity"],
        expected_state_identity=record["state_extraction_identity"],
    )

    assert len(applied["candidates"]) == 1

    assert applied["diagnostics"]["applied_resolutions"][0][
        "resolution_method"
    ] == "human_approved"

    assert glossary_candidate_semantic_fingerprint(
        applied["candidates"][0]
    ) == (
        "93f2606e6c66eba372654c4bbdd9252ef584582f9c29c21ec41fd2379c6dbd9a"
    )

    assert api_calls == []
    assert accounting_calls == []

    assert hashlib.sha256(
        chapter.read_bytes()
    ).hexdigest().upper() == (
        "B06B7B92B2F2AA2BC1E23A49E0203A37AED368E4589BCEDA9B7C0D6BFBCFB467"
    )

    assert all(
        hashlib.sha256(path.read_bytes()).hexdigest() == digest
        for path, digest in before.items()
    )