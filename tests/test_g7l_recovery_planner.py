"""G7L Chapter-1 auxiliary state-recovery planner/verifier tests.

Synthetic and self-contained (no network, no credentials, no external live
evidence directory): every fixture builds a tiny run root under ``tmp_path`` with
the real file SHAPES and the exact recorded values of the GOAT Run-1 Chapter-1
degraded-state incident.
"""

import copy
import hashlib
import json
import os
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

import benchmark_longitudinal_config as config
import benchmark_longitudinal_g7j_reasoning_budget as g7j
import benchmark_longitudinal_g7l_ch1_state_recovery_plan as g7l


RUN_ID = (
    "runs/01_deepseek_deepseek_v4_1_flash__ace_evolution__6cbb1d73c9a4"
)
NOVEL_ID = "ace_evolution"
MODEL_ID = "deepseek/deepseek-v4.1-flash"
PROFILE_ID = "historical_military"

# The exact recorded real degraded state committed by the defective Chapter-1
# auxiliary state pipeline: no glossary entries, the degraded continuity
# summary, and the single recorded issue.
REAL_DEGRADED_STATE = {
    "glossary": {},
    "continuity_summary": (
        "Chapter 1 committed with a degraded state-extraction result; "
        "continuity beyond Chapter 1 is unavailable until the auxiliary "
        "state is recovered."
    ),
    "issues": [
        {
            "chapter": 1,
            "issue": (
                "state_extraction returned no visible content "
                "(finish_reason=length at the 3000 ceiling)"
            ),
        }
    ],
}

# Exact recorded journal/ledger facts of the incident.
INCIDENT_STATE_EXTRACTION_COMPLETION_TOKENS = 3_000
INCIDENT_STATE_EXTRACTION_REASONING_TOKENS = 3_000
INCIDENT_REPAIR_COMPLETION_TOKENS = 118
INCIDENT_REPAIR_FABRICATED_STATE = {
    "new_terms": [],
    "continuity_summary": (
        "Chapter 1: fabricated continuity produced by repairing empty input."
    ),
    "issues": [],
}

CHAPTER_SOURCE_TEXT = "第一章 源文本"
COMMITTED_TRANSLATION_TEXT = "Chapter 1 English translation.\n"
CH1_SOURCE_SHA256 = hashlib.sha256(
    CHAPTER_SOURCE_TEXT.encode("utf-8")
).hexdigest()
SHA256_EMPTY = hashlib.sha256(b"").hexdigest()


def _canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _sha256_json(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_canonical(value), encoding="utf-8")


def _sealed(record):
    value = copy.deepcopy(record)
    value["record_sha256"] = _sha256_json(value)
    return value


def _journal_record(
    *,
    operation_kind,
    translation_sha256,
    response,
    request_sha256,
    completion_tokens,
):
    response = copy.deepcopy(response)
    response["stats"] = {
        "prompt_tokens": 6_120,
        "completion_tokens": completion_tokens,
        "cache_hit_tokens": 0,
        "cache_miss_tokens": 6_120,
        "cost_usd": 0.003,
        "rate_correct_cost_usd": 0.003,
        "pricing_version": "command-code-provider-peak-offpeak-v1",
    }
    record = {
        "schema_version": 1,
        "revision": "paid-request-journal-v1",
        "operation_kind": operation_kind,
        "novel_id": NOVEL_ID,
        "chapter": 1,
        "provider": "command_code_provider",
        "model": MODEL_ID,
        "profile_id": PROFILE_ID,
        "source_sha256": CH1_SOURCE_SHA256,
        "translation_sha256": translation_sha256,
        "request_sha256": request_sha256,
        "status": "response_recorded",
        "attempt_context": {"mode": "goat-g7f-stage1-paid-executor-v2"},
        "response": response,
        "response_sha256": _sha256_json(response),
        "error": {},
        "created_utc": "2026-09-22T05:25:55+00:00",
        "updated_utc": "2026-09-22T05:25:56+00:00",
    }
    return _sealed(record)


def _state_extraction_response():
    return {
        "content": "",
        "finish_reason": "length",
        "provider_response_id": "hist-se-1",
        "provider_response_model": MODEL_ID,
        "request_sha256": "b" * 64,
        "logical_operation": "state_extraction",
    }


def _state_json_repair_response():
    return {
        "content": _canonical(INCIDENT_REPAIR_FABRICATED_STATE),
        "finish_reason": "stop",
        "provider_response_id": "hist-repair-1",
        "provider_response_model": MODEL_ID,
        "request_sha256": "c" * 64,
        "logical_operation": "state_json_repair",
    }


def _ledger_records():
    return [
        {
            "send_index": 1,
            "logical_operation": "translation",
            "chapter": 1,
            "request_sha256": "a" * 64,
            "request_started_utc": "2026-09-22T05:10:00+00:00",
            "status": "accepted",
            "accepted_cost_usd": "0.007900000",
            "stats": {"completion_tokens": 11_000},
        },
        {
            "send_index": 2,
            "logical_operation": "state_extraction",
            "chapter": 1,
            "request_sha256": "b" * 64,
            "request_started_utc": "2026-09-22T05:20:00+00:00",
            "status": "accepted",
            "accepted_cost_usd": "0.003100000",
            "stats": {"completion_tokens": 3_000},
        },
        {
            "send_index": 3,
            "logical_operation": "state_json_repair",
            "chapter": 1,
            "request_sha256": "c" * 64,
            "request_started_utc": "2026-09-22T05:25:00+00:00",
            "status": "accepted",
            "accepted_cost_usd": "0.001237714",
            "stats": {"completion_tokens": 118},
        },
    ]


def _build_evidence(tmp_path, *, run_id=RUN_ID, monkeypatch, mutations=()):
    """Build one synthetic incident evidence root with the real file shapes.

    The two pinned byte-level facts that cannot be forged (a preimage of the real
    committed translation and QA digests) are bound to this fixture's own bytes
    through the module constants, while every other pinned literal is used
    verbatim. ``test_real_degraded_state_regression`` separately asserts that the
    real pinned literals are still exactly the frozen ones.
    """
    root = tmp_path / "evidence"
    run_root = root / Path(*run_id.split("/"))
    run_root.mkdir(parents=True)

    source_bytes = b"synthetic staged epub bytes"
    (run_root / "source").mkdir()
    (run_root / "source" / "novel.epub").write_bytes(source_bytes)
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()

    _write_json(
        run_root / "novel.json",
        {
            "id": NOVEL_ID,
            "title": "Ace Evolution",
            "source_language": "zh",
            "target_language": "en",
            "profile": PROFILE_ID,
            "final_chapter": 30,
            "audit_interval": 25,
            "chapter_numbering": "global",
            "source_type": "epub",
        },
    )

    run_identity = {
        "schema_version": 1,
        "g7c_revision": "goat-g7c-stage1-execution-integration-v1",
        "run_index": 1,
        "model_id": MODEL_ID,
        "novel_id": NOVEL_ID,
        "run_sha256": "9" * 64,
        "s0_identity_sha256": "8" * 64,
        "source_sha256": source_sha256,
        "chapter_library_sha256": "7" * 64,
        "profile": PROFILE_ID,
        "relative_root": run_id,
    }
    run_identity["identity_sha256"] = _sha256_json(run_identity)
    _write_json(run_root / "g7c_run_identity.json", run_identity)

    _write_json(
        run_root / "data" / "progress.json",
        {
            "completed": [1],
            "failed": [],
            "last_completed": 1,
            "next_chapter": 2,
            "total_prompt_tokens": 6_120,
            "total_completion_tokens": 3_118,
            "estimated_cost_usd": 0.0043,
            "pricing_version": "command-code-provider-peak-offpeak-v1",
        },
    )
    _write_json(run_root / "data" / "glossary.json", {})
    _write_json(
        run_root / "data" / "continuity.json",
        {
            "latest_chapter": 1,
            "summary": REAL_DEGRADED_STATE["continuity_summary"],
        },
    )
    _write_json(
        run_root / "data" / "issues.json",
        {"issues": copy.deepcopy(REAL_DEGRADED_STATE["issues"])},
    )

    (run_root / "output" / "chapters").mkdir(parents=True)
    committed = run_root / "output" / "chapters" / "0001.md"
    committed.write_text(COMMITTED_TRANSLATION_TEXT, encoding="utf-8")
    (run_root / "output" / "qa").mkdir(parents=True)
    qa = run_root / "output" / "qa" / "0001.json"
    _write_json(
        qa,
        {
            "chapter": 1,
            "commit_allowed": True,
            "issues": ["state_extraction returned no visible content"],
        },
    )

    journal_dir = run_root / "data" / "provider_request_journal"
    journal_dir.mkdir(parents=True)
    extraction_journal = journal_dir / "0001.g7c_state_extraction.json"
    _write_json(
        extraction_journal,
        _journal_record(
            operation_kind="state_extraction",
            translation_sha256=hashlib.sha256(
                COMMITTED_TRANSLATION_TEXT.encode("utf-8")
            ).hexdigest(),
            response=_state_extraction_response(),
            request_sha256="b" * 64,
            completion_tokens=INCIDENT_STATE_EXTRACTION_COMPLETION_TOKENS,
        ),
    )
    repair_journal = journal_dir / "0001.g7c_state_json_repair.json"
    _write_json(
        repair_journal,
        _journal_record(
            operation_kind="state_json_repair",
            # The recorded repair request was fed EMPTY malformed content.
            translation_sha256="",
            response=_state_json_repair_response(),
            request_sha256="c" * 64,
            completion_tokens=INCIDENT_REPAIR_COMPLETION_TOKENS,
        ),
    )

    ledger_dir = run_root / "data" / "g7f_invocation_ledgers"
    ledger_dir.mkdir(parents=True)
    ledger = {
        "schema_version": 1,
        "revision": "g7f-persisted-invocation-budget-v1",
        "plan_sha256": g7l.ORIGINAL_LEDGER_PLAN_SHA256,
        "approval_token": g7l.ORIGINAL_LEDGER_APPROVAL_TOKEN,
        "run_index": 1,
        "relative_root": run_id,
        "model_id": MODEL_ID,
        "novel_id": NOVEL_ID,
        "target_chapter": 1,
        "maximum_new_provider_sends": 6,
        "maximum_credit_equivalent_usd": "0.5",
        "new_provider_sends_started": (
            g7l.ORIGINAL_LEDGER_NEW_PROVIDER_SENDS_STARTED
        ),
        "accepted_credit_equivalent_usd": (
            g7l.ORIGINAL_LEDGER_ACCEPTED_CREDIT_EQUIVALENT_USD
        ),
        "records": _ledger_records(),
        "created_utc": "2026-09-22T05:00:00+00:00",
        "updated_utc": "2026-09-22T05:25:56+00:00",
    }
    ledger["ledger_sha256"] = _sha256_json(ledger)
    _write_json(ledger_dir / "0001.json", ledger)

    # Bind this fixture's un-forgeable byte-level pins to the module constants.
    monkeypatch.setattr(
        g7l,
        "COMMITTED_TRANSLATION_SHA256",
        hashlib.sha256(
            committed.read_bytes()
        ).hexdigest().upper(),
    )
    monkeypatch.setattr(
        g7l,
        "QA_SHA256",
        hashlib.sha256(qa.read_bytes()).hexdigest().upper(),
    )

    for relative, mutate in mutations:
        mutate(run_root / Path(relative))

    return SimpleNamespace(
        evidence_root=root,
        run_root=run_root,
        run_id=run_id,
        committed=committed,
        qa=qa,
        extraction_journal=extraction_journal,
        repair_journal=repair_journal,
        committed_sha256=hashlib.sha256(committed.read_bytes()).hexdigest(),
        qa_sha256=hashlib.sha256(qa.read_bytes()).hexdigest(),
        source_sha256=source_sha256,
    )


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    return _build_evidence(tmp_path, monkeypatch=monkeypatch)


def _snapshot(root):
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _tamper(plan, mutator):
    value = copy.deepcopy(plan)
    mutator(value)
    value["plan_sha256"] = _sha256_json(
        {key: item for key, item in value.items() if key != "plan_sha256"}
    )
    return value


def _write_plan(tmp_path, plan, name="plan.json"):
    path = tmp_path / "plans" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_canonical(plan), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# Planner boundary
# --------------------------------------------------------------------------


def test_planner_zero_provider_and_credential_free(evidence, monkeypatch):
    def _forbidden(*_args, **_kwargs):
        raise AssertionError(
            "G7L recovery planning must never touch the network or credentials"
        )

    for owner, name in (
        (socket, "socket"),
        (socket, "create_connection"),
        (socket, "getaddrinfo"),
        (os, "getenv"),
    ):
        monkeypatch.setattr(owner, name, _forbidden)
    monkeypatch.setattr(os, "environ", {})

    # The module provably contains no transport capability at all.
    g7l.assert_no_transport_symbols()
    source = Path(g7l.__file__).read_text(encoding="utf-8")
    for symbol in g7l._FORBIDDEN_TRANSPORT_SYMBOLS:
        assert symbol not in source
        assert not hasattr(g7l, symbol)
    # Ordinary frozen budget CONSTANTS (``DEFAULT_MAX_SENDS``,
    # ``ORIGINAL_LEDGER_NEW_PROVIDER_SENDS_STARTED``) are integers, not
    # capabilities: no module attribute may be a callable send/client object.
    assert not [
        name
        for name in dir(g7l)
        if ("send" in name.lower() or "client" in name.lower())
        and callable(getattr(g7l, name))
    ]
    assert all(
        not callable(getattr(g7l, name))
        for name in dir(g7l)
        if "socket" in name.lower()
        or name.lower().startswith(("open", "post", "request"))
    )

    plan = g7l.plan_ch1_state_recovery(
        evidence.evidence_root,
        evidence.run_id,
    )
    assert plan["provider_calls"] == 0
    assert plan["safety"] == {
        "provider_calls": 0,
        "provider_client_constructed": False,
        "credential_access": False,
        "paid_execution_authorized": False,
        "chapter_1_executed": False,
        "chapter_1_retranslated": False,
        "chapter_2_advanced": False,
        "state_fabricated": False,
        "historical_evidence_modified": False,
        "evidence_root_written": False,
        "transport_symbols_in_module": 0,
    }
    assert plan["operations"][0]["provider_calls_executed"] == 0
    assert plan["authorization"]["planner_emits_authorization"] is False

    summary = g7l.render_summary(plan)
    assert "BENCHMARK_PROVIDER_CALLS=0" in summary
    assert "CREDENTIAL_ACCESS=NO" in summary
    assert "PAID_EXECUTION_AUTHORIZED=NO" in summary
    assert "FRESH_OPERATOR_AUTHORIZATION_REQUIRED=YES" in summary
    assert "MAX_NEW_PROVIDER_SENDS=2" in summary
    assert g7l.g7l_policy_identity()["provider_boundary"] == {
        "provider_calls": 0,
        "provider_client_constructed": False,
        "credential_access": False,
        "chapter_execution": False,
        "retry_authorized": False,
        "transport_symbols_in_module": 0,
    }


def test_binding_payload_completeness(evidence):
    plan = g7l.plan_ch1_state_recovery(
        evidence.evidence_root,
        evidence.run_id,
    )
    payload = plan["recovery_binding_payload"]

    assert set(payload) == {
        "revision",
        "recovery_generation_id",
        "run_id",
        "novel_id",
        "chapter",
        "model_id",
        "source_sha256",
        "committed_translation_sha256",
        "qa_sha256",
        "progress",
        "state_extraction_journal_record_sha256",
        "state_json_repair_journal_record_sha256",
        "original_ch1_ledger_record",
        "degraded_state",
        "corrected_auxiliary_budget",
        "max_sends",
        "max_ce_usd",
    }
    assert payload["run_id"] == evidence.run_id
    assert payload["novel_id"] == NOVEL_ID
    assert payload["chapter"] == 1
    assert payload["model_id"] == MODEL_ID
    assert payload["source_sha256"] == evidence.source_sha256
    assert payload["committed_translation_sha256"] == (
        g7l.COMMITTED_TRANSLATION_SHA256
    )
    assert payload["qa_sha256"] == g7l.QA_SHA256
    assert payload["progress"] == {
        "completed": [1],
        "last_completed": 1,
        "next_chapter": 2,
    }
    assert payload["state_extraction_journal_record_sha256"] == (
        json.loads(evidence.extraction_journal.read_text(encoding="utf-8"))[
            "record_sha256"
        ]
    )
    assert payload["state_json_repair_journal_record_sha256"] == (
        json.loads(evidence.repair_journal.read_text(encoding="utf-8"))[
            "record_sha256"
        ]
    )
    ledger_record = payload["original_ch1_ledger_record"]
    assert ledger_record["plan_sha256"] == g7l.ORIGINAL_LEDGER_PLAN_SHA256
    assert ledger_record["approval_token"] == (
        g7l.ORIGINAL_LEDGER_APPROVAL_TOKEN
    )
    assert ledger_record["new_provider_sends_started"] == 3
    assert ledger_record["accepted_credit_equivalent_usd"] == "0.012237714"
    assert payload["corrected_auxiliary_budget"] == 4_000
    assert payload["max_sends"] == 2
    assert payload["max_ce_usd"] == "0.05"

    # The binding is exactly the canonical JSON hash of the payload.
    assert plan["recovery_binding_sha256"] == _sha256_json(payload)
    assert plan["recovery_generation_id"].startswith(
        g7l.RECOVERY_GENERATION_PREFIX
    )
    assert plan["recovery_generation_id"] != (
        g7l.PREVIOUS_RECOVERY_GENERATION_ID
    )
    assert plan["recovery_generation_id"] != "G7J24K_RECOVERY_20260922_230426"
    assert plan["authorization"]["bound_recovery_binding_sha256"] == (
        plan["recovery_binding_sha256"]
    )

    # The default ceilings are the frozen ones and drift is rejected.
    assert g7l.DEFAULT_MAX_SENDS == 2
    assert g7l.DEFAULT_MAX_CE_USD == "0.05"
    with pytest.raises(g7l.G7LRecoveryError, match="frozen"):
        g7l.plan_ch1_state_recovery(
            evidence.evidence_root,
            evidence.run_id,
            corrected_aux_budget=3_000,
        )
    with pytest.raises(g7l.G7LRecoveryError, match="positive integer"):
        g7l.plan_ch1_state_recovery(
            evidence.evidence_root,
            evidence.run_id,
            max_sends=0,
        )


def test_history_immutable_under_planner_and_verify(evidence, tmp_path):
    before = _snapshot(evidence.evidence_root)

    plan = g7l.plan_ch1_state_recovery(
        evidence.evidence_root,
        evidence.run_id,
    )
    g7l.render_summary(plan)
    plan_path = g7l.write_recovery_plan(
        plan,
        tmp_path / "plans" / "g7l_plan.json",
        evidence_root=evidence.evidence_root,
    )
    report = g7l.verify_recovery_plan(plan_path)

    assert report["status"] == "READY_FOR_FRESH_OPERATOR_AUTHORIZATION"
    assert report["provider_calls"] == 0
    assert report["credential_access"] is False
    assert report["chapter_1_executed"] is False
    assert report["evidence_root_written"] is False
    assert report["plan_sha256"] == plan["plan_sha256"]
    assert _snapshot(evidence.evidence_root) == before

    # The plan must never be writable inside the evidence root.
    with pytest.raises(g7l.G7LRecoveryError, match="evidence root"):
        g7l.write_recovery_plan(
            plan,
            evidence.run_root / "plan.json",
            evidence_root=evidence.evidence_root,
        )
    assert _snapshot(evidence.evidence_root) == before


def test_missing_evidence_fails_closed(tmp_path, monkeypatch):
    for relative in (
        "novel.json",
        "g7c_run_identity.json",
        "data/progress.json",
        "data/glossary.json",
        "data/continuity.json",
        "data/issues.json",
        "data/provider_request_journal/0001.g7c_state_extraction.json",
        "data/provider_request_journal/0001.g7c_state_json_repair.json",
        "data/g7f_invocation_ledgers/0001.json",
        "output/chapters/0001.md",
        "output/qa/0001.json",
        "source/novel.epub",
    ):
        case = tmp_path / relative.replace("/", "_")
        case.mkdir()
        built = _build_evidence(case, monkeypatch=monkeypatch)
        (built.run_root / Path(relative)).unlink()
        with pytest.raises(g7l.G7LRecoveryError):
            g7l.verify_historical_incident(
                built.evidence_root, built.run_id
            )

    built = _build_evidence(tmp_path / "unknown-run", monkeypatch=monkeypatch)
    with pytest.raises(g7l.G7LRecoveryError, match="run root missing"):
        g7l.verify_historical_incident(
            built.evidence_root, "runs/does_not_exist"
        )
    with pytest.raises(g7l.G7LRecoveryError, match="run_id"):
        g7l.verify_historical_incident(built.evidence_root, "../escape")
    with pytest.raises(g7l.G7LRecoveryError, match="evidence root missing"):
        g7l.verify_historical_incident(
            tmp_path / "absent-root", built.run_id
        )


def test_modified_evidence_fails_closed(tmp_path, monkeypatch):
    def _truncate_committed(path):
        path.write_text("tampered chapter\n", encoding="utf-8")

    def _stop_finish_reason(path):
        record = json.loads(path.read_text(encoding="utf-8"))
        record["response"]["finish_reason"] = "stop"
        record["response_sha256"] = _sha256_json(record["response"])
        path.write_text(_canonical(_sealed(record)), encoding="utf-8")

    def _advance_progress(path):
        progress = json.loads(path.read_text(encoding="utf-8"))
        progress["next_chapter"] = 3
        path.write_text(_canonical(progress), encoding="utf-8")

    def _shift_ledger_cost(path):
        ledger = json.loads(path.read_text(encoding="utf-8"))
        ledger["accepted_credit_equivalent_usd"] = "9.99"
        ledger.pop("ledger_sha256", None)
        ledger["ledger_sha256"] = _sha256_json(ledger)
        path.write_text(_canonical(ledger), encoding="utf-8")

    def _nonempty_glossary(path):
        path.write_text(
            _canonical({"entries": {"第一章": {"english": "Chapter One"}}}),
            encoding="utf-8",
        )

    def _populated_repair_input(path):
        record = json.loads(path.read_text(encoding="utf-8"))
        record["translation_sha256"] = hashlib.sha256(
            b"nonempty malformed json"
        ).hexdigest()
        path.write_text(_canonical(_sealed(record)), encoding="utf-8")

    for index, (relative, mutate) in enumerate(
        (
            ("output/chapters/0001.md", _truncate_committed),
            (
                "data/provider_request_journal/0001.g7c_state_extraction.json",
                _stop_finish_reason,
            ),
            ("data/progress.json", _advance_progress),
            ("data/g7f_invocation_ledgers/0001.json", _shift_ledger_cost),
            ("data/glossary.json", _nonempty_glossary),
            (
                "data/provider_request_journal/0001.g7c_state_json_repair.json",
                _populated_repair_input,
            ),
        )
    ):
        case = tmp_path / f"modified-{index}"
        case.mkdir()
        built = _build_evidence(
            case,
            monkeypatch=monkeypatch,
            mutations=((relative, mutate),),
        )
        with pytest.raises(g7l.G7LRecoveryError):
            g7l.verify_historical_incident(built.evidence_root, built.run_id)


# --------------------------------------------------------------------------
# Plan safety invariants
# --------------------------------------------------------------------------


def test_no_fixed_state_extraction_slot_use(evidence):
    plan = g7l.plan_ch1_state_recovery(
        evidence.evidence_root,
        evidence.run_id,
    )
    operations = plan["operations"]
    assert len(operations) == 1
    operation = operations[0]
    assert operation["operation_id"] == g7l.RECOVERY_OPERATION_ID
    assert operation["journal_kind"] == g7l.RECOVERY_OPERATION_ID
    assert operation["journal_kind"] == "g7l_ch1_state_recovery_repair"
    assert operation["journal_kind"] != (
        g7l.FIXED_STATE_EXTRACTION_JOURNAL_KIND
    )
    assert operation["journal_kind"] != g7l.FIXED_STATE_JSON_REPAIR_JOURNAL_KIND
    assert not operation["journal_kind"].startswith("g7c_")
    assert operation["reuses_fixed_state_extraction_slot"] is False
    assert operation["reuses_fixed_state_json_repair_slot"] is False
    assert operation["uses_lifecycle_operation_slot"] is False
    assert operation["journal_kind"] not in (
        config.PURE_RUN_LIFECYCLE_OPERATIONS
    )
    assert all("g7c_" not in value for value in json.dumps(operations).split())

    # The fixed slot appears only as historical evidence, never as a slot.
    assert g7l.FIXED_STATE_EXTRACTION_JOURNAL_KIND in json.dumps(
        plan["observed_evidence"]
    )
    identifier = g7l.RECOVERY_OPERATION_ID
    assert identifier not in config.PURE_RUN_LIFECYCLE_OPERATIONS
    namespace = g7l.g7l_policy_identity()["operation_namespace"]
    assert namespace["fixed_slot_reused"] is False
    assert namespace["fixed_slot_reused_by_g7l"] is False
    assert namespace["lifecycle_operation_slot_used"] is False


def test_no_ch1_retransmit(evidence, tmp_path):
    plan = g7l.plan_ch1_state_recovery(
        evidence.evidence_root,
        evidence.run_id,
    )
    scope = plan["chapter_scope"]
    assert scope["chapter"] == 1
    assert scope["committed_translation_sha256"] == (
        g7l.COMMITTED_TRANSLATION_SHA256
    )
    assert scope["chapter_1_retranslation_allowed"] is False
    assert scope["chapter_1_retransmit_allowed"] is False
    assert scope["translation_operation_present"] is False
    assert all(
        operation["logical_operation"]
        != config.LIFECYCLE_OPERATION_TRANSLATION
        for operation in plan["operations"]
    )
    assert "translation_operation" not in json.dumps(plan["operations"])

    # A plan that adds a Chapter-1 retranslation op fails closed.
    def _add_translation(value):
        value["operations"].append(
            {
                "operation_id": "g7c_translation",
                "journal_kind": "g7c_translation",
                "chapter": 1,
                "logical_operation": config.LIFECYCLE_OPERATION_TRANSLATION,
                "reuses_fixed_state_extraction_slot": False,
                "corrected_auxiliary_budget": 4_000,
            }
        )

    path = _write_plan(tmp_path, _tamper(plan, _add_translation))
    with pytest.raises(g7l.G7LRecoveryError, match="retranslation"):
        g7l.verify_recovery_plan(path)

    # A plan that claims the frozen committed translation bytes changed fails
    # closed on the binding recomputation.
    def _repoint_translation(value):
        value["chapter_scope"]["committed_translation_sha256"] = "0" * 64

    path = _write_plan(
        tmp_path,
        _tamper(plan, _repoint_translation),
        name="retranslate.json",
    )
    with pytest.raises(g7l.G7LRecoveryError, match="committed"):
        g7l.verify_recovery_plan(path)


def test_no_ch2_advance(evidence, tmp_path):
    plan = g7l.plan_ch1_state_recovery(
        evidence.evidence_root,
        evidence.run_id,
    )
    scope = plan["chapter_scope"]
    assert scope["allowed_chapters"] == [1]
    assert scope["next_chapter"] == 2
    assert scope["chapter_2_advance_allowed"] is False
    assert plan["post_recovery_pipeline"][
        "next_chapter_requires_a_separate_future_plan"
    ] is True

    def _widen_scope(value):
        value["chapter_scope"]["allowed_chapters"] = [1, 2]

    with pytest.raises(g7l.G7LRecoveryError, match="Chapter 1 only"):
        g7l.verify_recovery_plan(
            _write_plan(tmp_path, _tamper(plan, _widen_scope))
        )

    def _advance_operation(value):
        value["operations"][0]["chapter"] = 2

    with pytest.raises(g7l.G7LRecoveryError, match="Chapter 1"):
        g7l.verify_recovery_plan(
            _write_plan(
                tmp_path,
                _tamper(plan, _advance_operation),
                name="ch2.json",
            )
        )

    def _allow_advance(value):
        value["chapter_scope"]["chapter_2_advance_allowed"] = True

    with pytest.raises(g7l.G7LRecoveryError, match="forbid"):
        g7l.verify_recovery_plan(
            _write_plan(
                tmp_path,
                _tamper(plan, _allow_advance),
                name="advance.json",
            )
        )


def test_consumed_authorization_not_reusable(evidence, tmp_path):
    plan = g7l.plan_ch1_state_recovery(
        evidence.evidence_root,
        evidence.run_id,
    )
    authorization = plan["authorization"]
    assert authorization["active_authorization"] is None
    assert authorization["granted"] is False
    assert authorization["historical_consumed_plan_sha256"] == (
        g7l.ORIGINAL_LEDGER_PLAN_SHA256
    )
    assert authorization["historical_consumed_approval_token"] == (
        g7l.ORIGINAL_LEDGER_APPROVAL_TOKEN
    )
    assert authorization["historical_consumed_authorization_reusable"] is False
    assert authorization[
        "historical_consumed_executor_token_reuse_forbidden"
    ] is True

    def _reuse_consumed_authorization(value):
        value["authorization"]["active_authorization"] = {
            "plan_sha256": g7l.ORIGINAL_LEDGER_PLAN_SHA256,
            "approval_token": g7l.ORIGINAL_LEDGER_APPROVAL_TOKEN,
        }

    with pytest.raises(g7l.G7LRecoveryError, match="active authorization"):
        g7l.verify_recovery_plan(
            _write_plan(tmp_path, _tamper(plan, _reuse_consumed_authorization))
        )

    def _smuggle_token_elsewhere(value):
        value["operations"][0]["approval_token"] = (
            g7l.ORIGINAL_LEDGER_APPROVAL_TOKEN
        )

    with pytest.raises(
        g7l.G7LRecoveryError,
        match="must never be referenced as an active authorization",
    ):
        g7l.verify_recovery_plan(
            _write_plan(
                tmp_path,
                _tamper(plan, _smuggle_token_elsewhere),
                name="smuggled.json",
            )
        )

    def _mark_reusable(value):
        value["authorization"]["historical_consumed_authorization_reusable"] = (
            True
        )

    with pytest.raises(g7l.G7LRecoveryError, match="non-reusable"):
        g7l.verify_recovery_plan(
            _write_plan(
                tmp_path,
                _tamper(plan, _mark_reusable),
                name="reusable.json",
            )
        )


def test_fresh_authorization_required(evidence, tmp_path):
    plan = g7l.plan_ch1_state_recovery(
        evidence.evidence_root,
        evidence.run_id,
    )
    authorization = plan["authorization"]
    assert authorization["required"] is True
    assert authorization["granted"] is False
    assert authorization["bound_recovery_generation_id"] == (
        plan["recovery_generation_id"]
    )
    for field in (
        "recovery_generation_id",
        "recovery_binding_sha256",
        "chapter",
        "model_id",
        "corrected_auxiliary_budget",
        "maximum_new_provider_sends",
        "maximum_credit_equivalent_usd",
    ):
        assert field in authorization["required_future_authorization_fields"]

    def _grant(value):
        value["authorization"]["granted"] = True

    with pytest.raises(g7l.G7LRecoveryError, match="grant"):
        g7l.verify_recovery_plan(_write_plan(tmp_path, _tamper(plan, _grant)))

    def _drop_requirement(value):
        value["authorization"]["required"] = False

    with pytest.raises(g7l.G7LRecoveryError, match="authorization must be required"):
        g7l.verify_recovery_plan(
            _write_plan(
                tmp_path,
                _tamper(plan, _drop_requirement),
                name="not-required.json",
            )
        )

    def _unbind(value):
        value["authorization"]["required_future_authorization_fields"] = [
            "chapter"
        ]

    with pytest.raises(g7l.G7LRecoveryError, match="must bind"):
        g7l.verify_recovery_plan(
            _write_plan(tmp_path, _tamper(plan, _unbind), name="unbound.json")
        )


def test_atomic_fail_closed_apply_spec(evidence, tmp_path):
    plan = g7l.plan_ch1_state_recovery(
        evidence.evidence_root,
        evidence.run_id,
    )
    contract = plan["apply_contract"]
    assert contract["status"] == "described_not_executed"
    assert contract["executed"] is False
    assert contract["atomic"] is True
    assert contract["fail_closed"] is True
    assert contract["atomic_swap_required"] is True
    assert contract["single_writer_run_lock_required"] is True
    assert contract["partial_apply_forbidden"] is True
    assert contract["no_local_fabrication_of_state"] is True
    assert contract["progress_or_chapter_files_mutated"] is False
    assert contract["fields"] == [
        "data/glossary.json",
        "data/continuity.json",
        "data/issues.json",
    ]
    assert contract["on_any_failure"] == "abort_without_mutation"
    assert plan["operations"][0]["state_source_rule"].startswith(
        "state may only come from a Provider response"
    )

    for name, mutator in (
        ("executed", lambda value: value["apply_contract"].update(
            {"executed": True}
        )),
        ("atomic", lambda value: value["apply_contract"].update(
            {"atomic": False}
        )),
        ("fail_closed", lambda value: value["apply_contract"].update(
            {"fail_closed": False}
        )),
        ("partial", lambda value: value["apply_contract"].update(
            {"partial_apply_forbidden": False}
        )),
        ("fabrication", lambda value: value["apply_contract"].update(
            {"no_local_fabrication_of_state": False}
        )),
        ("fields", lambda value: value["apply_contract"].update(
            {"fields": ["data/glossary.json"]}
        )),
    ):
        with pytest.raises(g7l.G7LRecoveryError):
            g7l.verify_recovery_plan(
                _write_plan(
                    tmp_path,
                    _tamper(plan, mutator),
                    name=f"apply-{name}.json",
                )
            )


def test_ch1_output_and_qa_byte_identical(evidence, tmp_path):
    chapter_bytes = evidence.committed.read_bytes()
    qa_bytes = evidence.qa.read_bytes()

    plan = g7l.plan_ch1_state_recovery(
        evidence.evidence_root,
        evidence.run_id,
    )
    assert hashlib.sha256(chapter_bytes).hexdigest() == evidence.committed_sha256
    assert hashlib.sha256(qa_bytes).hexdigest() == evidence.qa_sha256
    assert plan["recovery_binding_payload"][
        "committed_translation_sha256"
    ].lower() == evidence.committed_sha256
    assert plan["recovery_binding_payload"]["qa_sha256"].lower() == (
        evidence.qa_sha256
    )
    assert plan["observed_evidence"]["committed_translation_sha256"] == (
        evidence.committed_sha256
    )
    assert plan["observed_evidence"]["qa_sha256"] == evidence.qa_sha256

    plan_path = g7l.write_recovery_plan(
        plan,
        tmp_path / "plans" / "u.json",
        evidence_root=evidence.evidence_root,
    )
    report = g7l.verify_recovery_plan(plan_path)
    assert report["committed_translation_sha256"] == (
        g7l.COMMITTED_TRANSLATION_SHA256
    )
    assert report["qa_sha256"] == g7l.QA_SHA256

    # The committed Chapter-1 output and its QA bytes are untouched.
    assert evidence.committed.read_bytes() == chapter_bytes
    assert evidence.qa.read_bytes() == qa_bytes

    def _repoint_qa(value):
        value["recovery_binding_payload"]["qa_sha256"] = "0" * 64

    with pytest.raises(g7l.G7LRecoveryError):
        g7l.verify_recovery_plan(
            _write_plan(
                tmp_path,
                _tamper(plan, _repoint_qa),
                name="qa-repointed.json",
            )
        )
    assert evidence.committed.read_bytes() == chapter_bytes
    assert evidence.qa.read_bytes() == qa_bytes


def test_real_degraded_state_regression(tmp_path, monkeypatch):
    """The real frozen incident values and degraded state are still pinned.

    The pinned byte-level literals are read BEFORE any fixture monkeypatches the
    module constants for the synthetic evidence root, so this test asserts the
    real frozen values rather than the synthetic binding.
    """
    # Byte-level pins of the real committed Chapter-1 output and its QA.
    assert g7l.COMMITTED_TRANSLATION_SHA256 == (
        "6FCE1FC5992B41A029BEC1AE358ACA03CE7037EE0FCE3881D7AFAC490417DBD2"
    )
    assert g7l.QA_SHA256 == (
        "4D7F65907258AFFD289A0AF544C1A34C16FC447CA03B4CA580A934C472573A32"
    )
    # The consumed executor authorization of the degraded Chapter 1.
    assert g7l.ORIGINAL_LEDGER_PLAN_SHA256 == (
        "f74b93f30b48e955be2f500e8ab6b604ccf0a183e2c848334c96dfc2b039bd90"
    )
    assert g7l.ORIGINAL_LEDGER_APPROVAL_TOKEN == (
        "0e392c30e0f5a9a1db99c30ec9f53adce01bdcf91db33a57e6dd6bfa7cf07a97"
    )
    assert g7l.ORIGINAL_LEDGER_NEW_PROVIDER_SENDS_STARTED == 3
    assert g7l.ORIGINAL_LEDGER_ACCEPTED_CREDIT_EQUIVALENT_USD == "0.012237714"
    assert g7l.PREVIOUS_RECOVERY_GENERATION_ID == (
        "G7J24K_RECOVERY_20260922_230423".replace("230423", "230426")
    )

    # Only now is the synthetic evidence root built: it binds the un-forgeable
    # byte-level pins to its own bytes, so the frozen literals above had to be
    # asserted before this point.
    evidence = _build_evidence(tmp_path, monkeypatch=monkeypatch)

    # The real incident facts re-derive the fail-closed classification: an empty,
    # length-terminated auxiliary response can never be repaired.
    incident = g7j.classify_auxiliary_response(
        logical_operation="state_extraction",
        content="",
        finish_reason="length",
        completion_tokens=INCIDENT_STATE_EXTRACTION_COMPLETION_TOKENS,
        max_completion_tokens=3_000,
        reasoning_tokens=INCIDENT_STATE_EXTRACTION_REASONING_TOKENS,
    )
    assert incident["status"] == (
        g7j.AUXILIARY_BUDGET_EXHAUSTED_EMPTY_CONTENT
    )
    assert incident["fail_closed"] is True
    assert incident["repair_allowed"] is False

    observed = g7l.verify_historical_incident(
        evidence.evidence_root, evidence.run_id
    )
    assert observed["state_extraction_classification"] == (
        g7j.AUXILIARY_BUDGET_EXHAUSTED_EMPTY_CONTENT
    )
    assert observed["state_extraction_completion_tokens"] == 3_000
    assert observed["state_extraction_finish_reason"] == "length"
    assert observed["state_json_repair_received_empty_input"] is True
    assert observed["state_json_repair_fabricated_state_sha256"] == (
        _sha256_json(INCIDENT_REPAIR_FABRICATED_STATE)
    )
    assert observed["degraded_state"]["glossary"] == {}
    assert observed["degraded_state"]["glossary_entries"] == 0
    assert observed["degraded_state"]["continuity_summary"] == (
        REAL_DEGRADED_STATE["continuity_summary"]
    )
    assert observed["degraded_state"]["issues"] == (
        REAL_DEGRADED_STATE["issues"]
    )
    assert observed["evidence_root_modified"] is False

    plan = g7l.plan_ch1_state_recovery(
        evidence.evidence_root,
        evidence.run_id,
    )
    assert plan["recovery_binding_payload"]["degraded_state"] == {
        "glossary": {},
        "continuity_summary": REAL_DEGRADED_STATE["continuity_summary"],
        "issues": REAL_DEGRADED_STATE["issues"],
    }
    assert plan["observed_evidence"]["new_provider_sends_started"] == 3
    assert plan["observed_evidence"]["accepted_credit_equivalent_usd"] == (
        "0.012237714"
    )
    assert plan["observed_evidence"]["ledger_plan_sha256"] == (
        g7l.ORIGINAL_LEDGER_PLAN_SHA256
    )
    assert plan["observed_evidence"]["ledger_approval_token"] == (
        g7l.ORIGINAL_LEDGER_APPROVAL_TOKEN
    )
    assert plan["corrected_auxiliary_budget"] == 4_000
    assert plan["production_auxiliary_identity_ceiling"] == 3_000

    # No fabrication: the plan never carries recovered state of its own. The
    # operation's declared source RULE and the frozen fixed-slot FLAG names
    # legitimately mention state, so the check targets recovered-state PAYLOAD
    # keys and the recorded fabricated-state content instead of the substring.
    for operation in plan["operations"]:
        assert not (
            set(operation)
            & {
                "state",
                "recovered_state",
                "glossary",
                "glossary_entries",
                "continuity_summary",
                "issues",
                "new_terms",
                "fabricated_state",
            }
        )
    assert _canonical(INCIDENT_REPAIR_FABRICATED_STATE) not in json.dumps(
        plan["operations"]
    )
