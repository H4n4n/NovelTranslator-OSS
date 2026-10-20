"""G7O Chapter-1 state-recovery EXECUTOR tests — zero API, zero credential.

Every fixture is synthetic and self-contained: a tiny run root under ``tmp_path``
with the real file SHAPES, a plan materialized by the frozen G7N planner, a fake
Provider client, and a guarded client factory / secret reader that raise if the
audited path ever tried to construct a client or read a credential. Network
primitives (``socket.*``) are monkeypatched to raise, so any accidental Provider
transport fails loudly instead of silently reaching the network.
"""

import copy
import hashlib
import json
import os
import socket
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

import benchmark_longitudinal_config as config
import benchmark_longitudinal_g7j_reasoning_budget as g7j
import benchmark_longitudinal_g7n_ch1_state_recovery_plan as g7n
import benchmark_longitudinal_g7o_ch1_state_recovery_executor as g7o
from production_paid_request_journal import (
    PaidRequestUncertainError,
    paid_request_journal_path,
    request_identity,
)


RUN_ID = "runs/01_deepseek_deepseek_v4_1_flash__ace_evolution__6cbb1d73c9a4"
NOVEL_ID = "ace_evolution"
MODEL_ID = "deepseek/deepseek-v4.1-flash"
PROFILE_ID = "historical_military"
FIXED_NOW = datetime(2026, 9, 23, 2, 30, 0, tzinfo=timezone.utc)
SHA256_EMPTY = hashlib.sha256(b"").hexdigest()

CHAPTER_SOURCE_TEXT = "第一章 源文本\n第二段：测试。"
COMMITTED_TRANSLATION_TEXT = "Chapter 1 English translation.\n"
COMMITTED_TRANSLATION_BYTES = COMMITTED_TRANSLATION_TEXT.encode("utf-8")
# The real historical committed Chapter-1 artifact is CRLF-terminated: the same
# text with "\r\n" newlines, whose raw bytes are therefore the authoritative
# byte-identity bound by the recovery plan.
COMMITTED_TRANSLATION_CRLF_TEXT = COMMITTED_TRANSLATION_TEXT.replace(
    "\n", "\r\n"
)
COMMITTED_TRANSLATION_CRLF_BYTES = COMMITTED_TRANSLATION_CRLF_TEXT.encode(
    "utf-8"
)
QA_DOCUMENT = {
    "chapter": 1,
    "commit_allowed": True,
    "issues": ["state_extraction returned no visible content"],
}

REAL_DEGRADED_CONTINUITY_SUMMARY = (
    "No malformed JSON content was provided, so there was nothing to repair."
)

RECOVERED_STATE = {
    "new_terms": [
        {
            "chinese": "第一章",
            "english": "Chapter One",
            "category": "specialized recurring term",
            "status": "provisional",
            "notes": "opening chapter marker",
        },
        {
            "chinese": "测试",
            "english": "Test",
            "category": "specialized recurring term",
            "status": "provisional",
            "notes": "",
        },
    ],
    "continuity_summary": "Chapter 1 establishes the recovered continuity state.",
    "issues": [],
}

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


def _canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _sha256_json(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_canonical(value), encoding="utf-8")


def _sealed(record):
    value = copy.deepcopy(record)
    value["record_sha256"] = _sha256_json(value)
    return value


def _clock():
    return FIXED_NOW


# --------------------------------------------------------------------------
# Synthetic evidence root (real file shapes)
# --------------------------------------------------------------------------


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
        "source_sha256": _sha256_text(CHAPTER_SOURCE_TEXT),
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
            "logical_operation": config.LIFECYCLE_OPERATION_TRANSLATION,
            "chapter": 1,
            "request_sha256": "a" * 64,
            "request_started_utc": "2026-09-22T05:10:00+00:00",
            "status": "accepted",
            "accepted_cost_usd": "0.007900000",
            "stats": {"completion_tokens": 11_000},
        },
        {
            "send_index": 2,
            "logical_operation": config.LIFECYCLE_OPERATION_STATE_EXTRACTION,
            "chapter": 1,
            "request_sha256": "b" * 64,
            "request_started_utc": "2026-09-22T05:20:00+00:00",
            "status": "accepted",
            "accepted_cost_usd": "0.003100000",
            "stats": {"completion_tokens": 3_000},
        },
        {
            "send_index": 3,
            "logical_operation": config.LIFECYCLE_OPERATION_STATE_JSON_REPAIR,
            "chapter": 1,
            "request_sha256": "c" * 64,
            "request_started_utc": "2026-09-22T05:25:00+00:00",
            "status": "accepted",
            "accepted_cost_usd": "0.001237714",
            "stats": {"completion_tokens": 118},
        },
    ]


def _build_terminal_evidence(run_root, monkeypatch):
    """Build the terminal G7M recovery evidence the G7N plan binds.

    The real terminal G7M recovery is byte-frozen historical evidence. This
    synthetic fixture rebuilds its SHAPE (one accepted state_extraction send,
    4,000/4,000 tokens, empty visible content, fail-closed classification) and
    binds the module's pinned raw file-byte digests to the fixture's own bytes,
    while independently preserving the documents' canonical self-hashes.
    """
    terminal_ledger_dir = run_root / "data" / "g7m_recovery_ledgers"
    terminal_ledger_dir.mkdir(parents=True, exist_ok=True)
    terminal_ledger = {
        "schema_version": 1,
        "revision": "g7m-ch1-state-recovery-ledger-v1",
        "logical_operation": "state_extraction",
        "recovery_generation_id": "G7J24K_RECOVERY_20260922_230426",
        "recovery_binding_sha256": "1" * 64,
        "plan_sha256": "2" * 64,
        "run_id": RUN_ID,
        "novel_id": NOVEL_ID,
        "model_id": MODEL_ID,
        "target_chapter": 1,
        "maximum_new_provider_sends": 1,
        "maximum_credit_equivalent_usd": "0.05",
        "new_provider_sends_started": 1,
        "accepted_credit_equivalent_usd": "0.012237714",
        "records": [
            {
                "send_index": 1,
                "logical_operation": "state_extraction",
                "chapter": 1,
                "request_sha256": "d" * 64,
                "request_started_utc": "2026-09-22T23:00:00+00:00",
                "status": "accepted",
                "conservative_cost_bound_usd": "0.02",
                "accepted_cost_usd": "0.012237714",
                "stats": {"completion_tokens": 4_000},
                "error": {},
            }
        ],
        "created_utc": "2026-09-22T23:00:00+00:00",
        "updated_utc": "2026-09-22T23:04:26+00:00",
    }
    terminal_ledger["ledger_sha256"] = _sha256_json(terminal_ledger)
    terminal_ledger_path = terminal_ledger_dir / "0001.json"
    _write_json(terminal_ledger_path, terminal_ledger)
    ledger_file_digest = hashlib.sha256(
        terminal_ledger_path.read_bytes()
    ).hexdigest()
    terminal_journal_path = (
        run_root
        / "data"
        / "provider_request_journal"
        / "0001.g7l_ch1_state_recovery_repair.json"
    )
    terminal_response = {
        "content": "",
        "finish_reason": "length",
        "provider_response_id": "g7m-terminal-1",
        "provider_response_model": MODEL_ID,
        "request_sha256": "d" * 64,
        "logical_operation": "state_extraction",
        "journal_kind": "g7l_ch1_state_recovery_repair",
        "stats": {
            "prompt_tokens": 6_120,
            "completion_tokens": 4_000,
            "reasoning_tokens": 4_000,
            "cache_hit_tokens": 0,
            "cache_miss_tokens": 6_120,
            "cost_usd": 0.012,
            "rate_correct_cost_usd": 0.012,
            "pricing_version": "command-code-provider-peak-offpeak-v1",
        },
    }
    terminal_record = {
        "schema_version": 1,
        "revision": "paid-request-journal-v1",
        "operation_kind": "g7l_ch1_state_recovery_repair",
        "novel_id": NOVEL_ID,
        "chapter": 1,
        "provider": "command_code_provider",
        "model": MODEL_ID,
        "profile_id": PROFILE_ID,
        "source_sha256": _sha256_text(CHAPTER_SOURCE_TEXT),
        "translation_sha256": _sha256_text(COMMITTED_TRANSLATION_TEXT),
        "request_sha256": "d" * 64,
        "status": "response_recorded",
        "attempt_context": {"mode": "goat-g7m-ch1-state-recovery-executor-v1"},
        "response": terminal_response,
        "response_sha256": _sha256_json(terminal_response),
        "error": {},
        "created_utc": "2026-09-22T23:00:00+00:00",
        "updated_utc": "2026-09-22T23:04:26+00:00",
    }
    terminal_record["record_sha256"] = _sha256_json(terminal_record)
    _write_json(terminal_journal_path, terminal_record)
    journal_file_digest = hashlib.sha256(
        terminal_journal_path.read_bytes()
    ).hexdigest()
    ledger_digest = _sha256_json(
        {
            key: item
            for key, item in terminal_ledger.items()
            if key != "ledger_sha256"
        }
    )
    journal_digest = _sha256_json(
        {
            key: item
            for key, item in terminal_record.items()
            if key != "record_sha256"
        }
    )
    assert ledger_file_digest != ledger_digest
    assert journal_file_digest != journal_digest
    monkeypatch.setattr(
        g7n, "G7M_TERMINAL_LEDGER_SHA256", ledger_file_digest.upper()
    )
    monkeypatch.setattr(
        g7n, "G7M_TERMINAL_JOURNAL_SHA256", journal_file_digest.upper()
    )
    real_verify_terminal = getattr(
        g7n,
        "_G7O_TEST_ORIGINAL_VERIFY_TERMINAL",
        g7n.verify_terminal_recovery_incident,
    )
    g7n._G7O_TEST_ORIGINAL_VERIFY_TERMINAL = real_verify_terminal

    def _verify_terminal_with_fixture_pins(evidence_root, run_id):
        return real_verify_terminal(
            evidence_root,
            run_id,
            expected_ledger_sha256=ledger_file_digest.upper(),
            expected_journal_sha256=journal_file_digest.upper(),
        )

    monkeypatch.setattr(
        g7n,
        "verify_terminal_recovery_incident",
        _verify_terminal_with_fixture_pins,
    )
    return terminal_ledger_path, terminal_journal_path


def _build_evidence(
    base, *, monkeypatch, committed_bytes=COMMITTED_TRANSLATION_BYTES
):
    """Build one synthetic incident evidence root with the real file shapes.

    The two pinned byte-level facts that cannot be forged are bound to this
    fixture's own bytes through the module constants, exactly like the G7N
    planner tests.
    """
    root = base / "evidence"
    run_root = root / Path(*RUN_ID.split("/"))
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
        "relative_root": RUN_ID,
    }
    run_identity["identity_sha256"] = _sha256_json(run_identity)
    _write_json(run_root / "g7c_run_identity.json", run_identity)

    # The production chapter library holds the committed Chapter-1 source text.
    _write_json(
        run_root / "data" / "chapters.json",
        [
            {
                "chapter": 1,
                "production_index": 1,
                "volume_index": 1,
                "local_chapter_number": 1,
                "local_chapter_start": 1,
                "local_chapter_end": 1,
                "heading": "第一章",
                "title": "第一章",
                "character_count": len(CHAPTER_SOURCE_TEXT),
                "paragraph_count": 2,
                "source_file": "chapter_0001.xhtml",
                "text": CHAPTER_SOURCE_TEXT,
            }
        ],
    )

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
            "summary": REAL_DEGRADED_CONTINUITY_SUMMARY,
        },
    )
    _write_json(
        run_root / "data" / "issues.json",
        {
            "issues": [
                {
                    "chapter": 1,
                    "type": "model_flag",
                    "details": "No JSON input was supplied.",
                }
            ]
        },
    )

    (run_root / "output" / "chapters").mkdir(parents=True)
    committed = run_root / "output" / "chapters" / "0001.md"
    # The committed Chapter-1 artifact is written as its EXACT pinned bytes: a
    # text-mode write would translate "\n" to os.linesep on Windows, and the
    # executor re-verifies that the recovered text re-encodes to the raw-file
    # SHA, which a newline-normalized read can never satisfy for a CRLF file.
    # The default here stays LF; the CRLF fixture pins CRLF bytes instead.
    committed.write_bytes(committed_bytes)
    (run_root / "output" / "qa").mkdir(parents=True)
    qa = run_root / "output" / "qa" / "0001.json"
    _write_json(qa, QA_DOCUMENT)

    journal_dir = run_root / "data" / "provider_request_journal"
    journal_dir.mkdir(parents=True)
    extraction_journal = journal_dir / "0001.g7c_state_extraction.json"
    _write_json(
        extraction_journal,
        _journal_record(
            operation_kind=config.LIFECYCLE_OPERATION_STATE_EXTRACTION,
            translation_sha256=_sha256_text(COMMITTED_TRANSLATION_TEXT),
            response=_state_extraction_response(),
            request_sha256="b" * 64,
            completion_tokens=INCIDENT_STATE_EXTRACTION_COMPLETION_TOKENS,
        ),
    )
    repair_journal = journal_dir / "0001.g7c_state_json_repair.json"
    _write_json(
        repair_journal,
        _journal_record(
            operation_kind=config.LIFECYCLE_OPERATION_STATE_JSON_REPAIR,
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
        "plan_sha256": g7n.ORIGINAL_LEDGER_PLAN_SHA256,
        "approval_token": g7n.ORIGINAL_LEDGER_APPROVAL_TOKEN,
        "run_index": 1,
        "relative_root": RUN_ID,
        "model_id": MODEL_ID,
        "novel_id": NOVEL_ID,
        "target_chapter": 1,
        "maximum_new_provider_sends": 6,
        "maximum_credit_equivalent_usd": "0.5",
        "new_provider_sends_started": (
            g7n.ORIGINAL_LEDGER_NEW_PROVIDER_SENDS_STARTED
        ),
        "accepted_credit_equivalent_usd": (
            g7n.ORIGINAL_LEDGER_ACCEPTED_CREDIT_EQUIVALENT_USD
        ),
        "records": _ledger_records(),
        "created_utc": "2026-09-22T05:00:00+00:00",
        "updated_utc": "2026-09-22T05:25:56+00:00",
    }
    ledger["ledger_sha256"] = _sha256_json(ledger)
    _write_json(ledger_dir / "0001.json", ledger)

    terminal_ledger_path, terminal_journal_path = _build_terminal_evidence(
        run_root, monkeypatch
    )

    # Bind this fixture's un-forgeable byte-level pins to the module constants.
    # G7N verifies these pins with `_sha256_file` (raw on-disk bytes), so the
    # pins must be derived from the bytes the file actually holds: a text write
    # translates "\n" to os.linesep on Windows, so the source-string digest
    # would no longer be a preimage of the file. Exactly like the G7N planner
    # tests.
    committed_file_sha256 = hashlib.sha256(committed.read_bytes()).hexdigest()
    qa_file_sha256 = hashlib.sha256(qa.read_bytes()).hexdigest()
    monkeypatch.setattr(
        g7n,
        "COMMITTED_TRANSLATION_SHA256",
        committed_file_sha256.upper(),
    )
    monkeypatch.setattr(g7n, "QA_SHA256", qa_file_sha256.upper())

    return SimpleNamespace(
        evidence_root=root,
        run_root=run_root,
        run_id=RUN_ID,
        committed=committed,
        qa=qa,
        extraction_journal=extraction_journal,
        repair_journal=repair_journal,
        historical_ledger=ledger_dir / "0001.json",
        terminal_ledger=terminal_ledger_path,
        terminal_journal=terminal_journal_path,
        committed_sha256=committed_file_sha256,
        qa_sha256=qa_file_sha256,
        source_sha256=source_sha256,
        source_chapter_sha256=_sha256_text(CHAPTER_SOURCE_TEXT),
    )


def _prepare(
    base,
    *,
    monkeypatch,
    mutations=(),
    committed_bytes=COMMITTED_TRANSLATION_BYTES,
):
    """Build evidence, materialize the G7N plan, THEN apply mutations."""
    built = _build_evidence(
        base, monkeypatch=monkeypatch, committed_bytes=committed_bytes
    )
    plan = g7n.plan_ch1_state_recovery(built.evidence_root, built.run_id)
    plan_path = g7n.write_recovery_plan(
        plan,
        base / "plans" / "g7n_plan.json",
        evidence_root=built.evidence_root,
    )
    built.plan = plan
    built.plan_path = plan_path
    for relative, mutate in mutations:
        mutate(built.run_root / Path(relative))
    return built


def _prepare_crlf(base, *, monkeypatch):
    """Materialize the same fixture with the real CRLF committed artifact."""
    built = _prepare(
        base,
        monkeypatch=monkeypatch,
        committed_bytes=COMMITTED_TRANSLATION_CRLF_BYTES,
    )
    assert built.committed.read_bytes() == COMMITTED_TRANSLATION_CRLF_BYTES
    assert g7n.COMMITTED_TRANSLATION_SHA256.lower() == hashlib.sha256(
        COMMITTED_TRANSLATION_CRLF_BYTES
    ).hexdigest()
    return built


@pytest.fixture
def env(tmp_path, monkeypatch):
    return _prepare(tmp_path, monkeypatch=monkeypatch)


@pytest.fixture
def crlf_env(tmp_path, monkeypatch):
    return _prepare_crlf(tmp_path, monkeypatch=monkeypatch)


def _preflight_kwargs(
    built,
    *,
    plan=None,
    plan_path=None,
    plan_sha256=None,
    generation=None,
    binding=None,
):
    plan = plan if plan is not None else built.plan
    return {
        "evidence_root": built.evidence_root,
        "run_id": built.run_id,
        "plan_path": plan_path if plan_path is not None else built.plan_path,
        "expected_plan_sha256": (
            plan_sha256 if plan_sha256 is not None else plan["plan_sha256"]
        ),
        "expected_recovery_generation_id": (
            generation
            if generation is not None
            else plan["recovery_generation_id"]
        ),
        "expected_recovery_binding_sha256": (
            binding
            if binding is not None
            else plan["recovery_binding_sha256"]
        ),
    }


@pytest.fixture
def preflight(env):
    return g7o.preflight_ch1_state_recovery(**_preflight_kwargs(env))


def _authorization(preflight, **overrides):
    value = {
        "operator_authorized": True,
        "recovery_generation_id": preflight["recovery_generation_id"],
        "recovery_binding_sha256": preflight["recovery_binding_sha256"],
        "run_id": preflight["run_id"],
        "novel_id": preflight["novel_id"],
        "chapter": preflight["chapter"],
        "model_id": preflight["model_id"],
        "corrected_auxiliary_budget": preflight[
            "corrected_auxiliary_budget"
        ],
        "maximum_new_provider_sends": preflight[
            "maximum_new_provider_sends"
        ],
        "maximum_credit_equivalent_usd": preflight[
            "maximum_credit_equivalent_usd"
        ],
    }
    value.update(overrides)
    return value


# --------------------------------------------------------------------------
# Fakes and guards
# --------------------------------------------------------------------------


def _provider_response(
    content,
    *,
    finish_reason="stop",
    completion_tokens=120,
    prompt_tokens=6_120,
    reasoning_tokens=None,
    model=MODEL_ID,
    response_id="recovery-response-1",
):
    usage = SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        prompt_tokens_details=SimpleNamespace(cached_tokens=0),
    )
    if reasoning_tokens is not None:
        usage.completion_tokens_details = SimpleNamespace(
            reasoning_tokens=reasoning_tokens
        )
    return SimpleNamespace(
        id=response_id,
        model=model,
        choices=[
            SimpleNamespace(
                finish_reason=finish_reason,
                message=SimpleNamespace(content=content),
            )
        ],
        usage=usage,
    )


def _fake_client(responses, *, on_call=None):
    """A fake Command Code client whose scripted outcomes are consumed in order."""
    client = SimpleNamespace()
    client.calls = []

    def create(**kwargs):
        if on_call is not None:
            on_call(kwargs)
        client.calls.append(kwargs)
        if not responses:
            raise AssertionError(
                "the recovery executor made an unexpected extra Provider call"
            )
        outcome = responses.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    client.chat = SimpleNamespace(
        completions=SimpleNamespace(create=create)
    )
    return client


def _guards(monkeypatch):
    """Install zero-network / zero-credential guards and return their counters."""
    calls = {"client_factory": 0, "secret_reader": 0}

    def _forbidden(*_args, **_kwargs):
        raise AssertionError(
            "the G7O recovery boundary must never touch the network"
        )

    for owner, name in (
        (socket, "socket"),
        (socket, "create_connection"),
        (socket, "getaddrinfo"),
    ):
        monkeypatch.setattr(owner, name, _forbidden)

    def guarded_client_factory(**_kwargs):
        calls["client_factory"] += 1
        raise AssertionError(
            "no Provider client may be constructed on this path"
        )

    def guarded_secret_reader():
        calls["secret_reader"] += 1
        raise AssertionError(
            "no credential may be read on this path"
        )

    return calls, guarded_client_factory, guarded_secret_reader


def _journal_dir(built):
    return built.run_root / "data" / "provider_request_journal"


def _ledger_path(built):
    return g7o.recovery_ledger_path(built.run_root)


def _snapshot(root):
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _state_bytes(built):
    return {
        relative: (built.run_root / relative).read_bytes()
        for relative in g7o.APPLY_FIELDS
    }


def _run(built, preflight, responses, *, on_call=None, clock=_clock):
    client = _fake_client(responses, on_call=on_call)
    outcome = g7o.run_recovery_request(
        preflight=preflight,
        recovery_ledger_path=_ledger_path(built),
        journal_dir=_journal_dir(built),
        provider_client=client,
        clock=clock,
    )
    return outcome, client


def _run_success(built, preflight, *, state=None, first_content=None):
    """Run one accepted recovery (optionally through the bounded repair)."""
    state = state if state is not None else RECOVERED_STATE
    if first_content is None:
        responses = [_provider_response(_canonical(state))]
    else:
        responses = [
            _provider_response(first_content),
            _provider_response(_canonical(state)),
        ]
    outcome, client = _run(built, preflight, responses)
    assert outcome["status"] == g7o.RECOVERY_ACCEPTED_STATUS
    return outcome, client


def _execute_success(built, preflight, *, state=None, first_content=None):
    state = state if state is not None else RECOVERED_STATE
    if first_content is None:
        responses = [_provider_response(_canonical(state))]
    else:
        responses = [
            _provider_response(first_content),
            _provider_response(_canonical(state)),
        ]
    client = _fake_client(responses)
    result = g7o.execute_ch1_state_recovery(
        **_preflight_kwargs(built),
        authorization=_authorization(preflight),
        authorize_paid_run=True,
        secret_reader=lambda: "synthetic-test-key",
        client_factory=lambda **_kwargs: client,
        clock=_clock,
    )
    return result, client


def _tamper(plan, mutator):
    value = copy.deepcopy(plan)
    mutator(value)
    value["plan_sha256"] = _sha256_json(
        {key: item for key, item in value.items() if key != "plan_sha256"}
    )
    return value


def _write_plan(base, plan, name="tampered.json"):
    """Write a tampered plan copy into the fixture's own plan directory.

    ``base`` is either the synthetic evidence base directory or a built ``env``
    fixture, so that callers may pass either form.
    """
    root = (
        Path(base.evidence_root).parent
        if isinstance(base, SimpleNamespace)
        else Path(base)
    )
    path = root / "plans" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_canonical(plan), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# 1. Preflight: zero provider, zero credential
# --------------------------------------------------------------------------


def test_preflight_zero_provider_zero_credential(env, monkeypatch):
    calls, factory, reader = _guards(monkeypatch)
    monkeypatch.setattr(os, "getenv", factory)
    before = _snapshot(env.evidence_root)

    assert g7o.assert_no_translation_or_chapter2_path() is None
    preflight = g7o.preflight_ch1_state_recovery(**_preflight_kwargs(env))

    assert preflight["status"] == "PREFLIGHT_OK"
    assert preflight["provider_calls"] == 0
    assert preflight["provider_client_constructed"] is False
    assert preflight["credential_access"] is False
    assert preflight["chapter_1_translation_retry"] is False
    assert preflight["chapter_2_execution"] is False
    assert preflight["plan_requires_rematerialization"] is False
    assert preflight["logical_operation"] == g7o.RECOVERY_LOGICAL_OPERATION
    assert preflight["journal_kind"] == g7o.RECOVERY_JOURNAL_KIND
    assert preflight["corrected_auxiliary_budget"] == 8_000
    assert preflight["maximum_new_provider_sends"] == 2
    assert preflight["maximum_credit_equivalent_usd"] == "0.05"
    assert preflight["consumed_authorization_reusable"] is False
    assert preflight["chapter"] == 1
    assert preflight["model_id"] == MODEL_ID
    assert preflight["novel_id"] == NOVEL_ID
    assert preflight["profile_id"] == PROFILE_ID
    assert preflight["progress"] == {
        "completed": [1],
        "last_completed": 1,
        "next_chapter": 2,
    }
    assert preflight["source_sha256"] == env.source_sha256
    assert preflight["source_chapter_sha256"] == env.source_chapter_sha256
    assert preflight["source_chapter_text"] == CHAPTER_SOURCE_TEXT
    assert preflight["committed_translation_text"] == COMMITTED_TRANSLATION_TEXT
    assert preflight["committed_translation_sha256"] == env.committed_sha256
    assert preflight["qa_sha256"] == env.qa_sha256
    assert preflight["degraded_state"]["glossary"] == {}
    assert preflight["degraded_state"]["continuity_summary"] == (
        REAL_DEGRADED_CONTINUITY_SUMMARY
    )
    assert preflight["original_journals"]["state_extraction"][
        "record_sha256"
    ] == json.loads(env.extraction_journal.read_text(encoding="utf-8"))[
        "record_sha256"
    ]
    assert preflight["original_journals"]["state_json_repair"][
        "record_sha256"
    ] == json.loads(env.repair_journal.read_text(encoding="utf-8"))[
        "record_sha256"
    ]
    assert preflight["original_ledger"]["plan_sha256"] == (
        g7n.ORIGINAL_LEDGER_PLAN_SHA256
    )
    assert preflight["original_ledger"]["approval_token"] == (
        g7n.ORIGINAL_LEDGER_APPROVAL_TOKEN
    )
    assert preflight["original_ledger"]["new_provider_sends_started"] == 3
    assert preflight["original_ledger"][
        "accepted_credit_equivalent_usd"
    ] == "0.012237714"
    assert set(preflight["required_fresh_authorization_fields"]) == set(
        g7o.RECOVERY_AUTHORIZATION_FIELDS
    ) | {"operator_authorized"}

    summary = g7o.recovery_summary(preflight)
    for marker in (
        "BENCHMARK_PROVIDER_CALLS=0",
        "CREDENTIAL_ACCESS=NO",
        "API_KEY_ACCESS=NO",
        "PROVIDER_CLIENT_CONSTRUCTED=NO",
        "PAID_EXECUTION_AUTHORIZED=NO",
        "CHAPTER_1_TRANSLATION_RETRY=NO",
        "CHAPTER_2_EXECUTION=NO",
        "REQUIRES_FRESH_OPERATOR_AUTHORIZATION=YES",
        "PLAN_REQUIRES_REMATERIALIZATION=NO",
        "CONSUMED_AUTHORIZATION_REUSABLE=NO",
    ):
        assert marker in summary

    assert calls == {"client_factory": 0, "secret_reader": 0}
    assert _snapshot(env.evidence_root) == before


# --------------------------------------------------------------------------
# 2. Exact plan SHA / generation / binding are required
# --------------------------------------------------------------------------


def test_exact_plan_generation_and_binding_required(env):
    with pytest.raises(g7o.G7ORecoveryError, match="generation id"):
        g7o.preflight_ch1_state_recovery(
            **_preflight_kwargs(env, generation="G7O_CH1_STATE_RECOVERY_OTHER")
        )
    with pytest.raises(g7o.G7ORecoveryError, match="recovery binding"):
        g7o.preflight_ch1_state_recovery(
            **_preflight_kwargs(env, binding="0" * 64)
        )
    with pytest.raises(g7o.G7ORecoveryError, match="plan SHA"):
        g7o.preflight_ch1_state_recovery(
            **_preflight_kwargs(env, plan_sha256="0" * 64)
        )

    preflight = g7o.preflight_ch1_state_recovery(**_preflight_kwargs(env))
    assert preflight["plan_sha256"] == env.plan["plan_sha256"]
    assert preflight["recovery_generation_id"] == (
        env.plan["recovery_generation_id"]
    )
    assert preflight["recovery_binding_sha256"] == (
        env.plan["recovery_binding_sha256"]
    )
    assert preflight["recovery_binding_sha256"] == _sha256_json(
        env.plan["recovery_binding_payload"]
    )

    # A plan file tampered after materialization must never pass: its envelope
    # SHA no longer recomputes.
    path = _write_plan(env, {**env.plan, "maximum_new_provider_sends": 9})
    with pytest.raises(g7o.G7ORecoveryError):
        g7o.preflight_ch1_state_recovery(
            **_preflight_kwargs(env, plan_path=path)
        )


# --------------------------------------------------------------------------
# 3/4. Wrong plan SHA or wrong evidence byte fails BEFORE credential access
# --------------------------------------------------------------------------


def test_wrong_plan_sha_fails_before_credential_access(env, monkeypatch):
    calls, factory, reader = _guards(monkeypatch)
    authorization = _authorization(
        g7o.preflight_ch1_state_recovery(**_preflight_kwargs(env))
    )

    with pytest.raises(g7o.G7ORecoveryError, match="plan SHA"):
        g7o.execute_ch1_state_recovery(
            **_preflight_kwargs(env, plan_sha256="0" * 64),
            authorization=authorization,
            authorize_paid_run=True,
            secret_reader=reader,
            client_factory=factory,
        )
    assert calls == {"client_factory": 0, "secret_reader": 0}

    # The explicit paid-run flag is required before anything paid can happen.
    with pytest.raises(g7o.G7ORecoveryError, match="authorize_paid_run"):
        g7o.execute_ch1_state_recovery(
            **_preflight_kwargs(env),
            authorization=authorization,
            authorize_paid_run=False,
            secret_reader=reader,
            client_factory=factory,
        )
    assert calls == {"client_factory": 0, "secret_reader": 0}


def test_wrong_evidence_byte_fails_before_credential_access(
    tmp_path, monkeypatch
):
    def _truncate_committed(path):
        path.write_text("tampered chapter\n", encoding="utf-8")

    def _retouch_qa(path):
        document = json.loads(path.read_text(encoding="utf-8"))
        document["commit_allowed"] = False
        path.write_text(_canonical(document), encoding="utf-8")

    def _advance_progress(path):
        progress = json.loads(path.read_text(encoding="utf-8"))
        progress["next_chapter"] = 3
        path.write_text(_canonical(progress), encoding="utf-8")

    def _stop_finish_reason(path):
        record = json.loads(path.read_text(encoding="utf-8"))
        record["response"]["finish_reason"] = "stop"
        record["response_sha256"] = _sha256_json(record["response"])
        path.write_text(_canonical(_sealed(record)), encoding="utf-8")

    def _populated_repair_input(path):
        record = json.loads(path.read_text(encoding="utf-8"))
        record["translation_sha256"] = _sha256_text("nonempty malformed json")
        path.write_text(_canonical(_sealed(record)), encoding="utf-8")

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

    def _missing_chapter_library(path):
        path.unlink()

    calls, factory, reader = _guards(monkeypatch)
    for index, (relative, mutate) in enumerate(
        (
            ("output/chapters/0001.md", _truncate_committed),
            ("output/qa/0001.json", _retouch_qa),
            ("data/progress.json", _advance_progress),
            (
                "data/provider_request_journal/0001.g7c_state_extraction.json",
                _stop_finish_reason,
            ),
            (
                "data/provider_request_journal/0001.g7c_state_json_repair.json",
                _populated_repair_input,
            ),
            ("data/g7f_invocation_ledgers/0001.json", _shift_ledger_cost),
            ("data/glossary.json", _nonempty_glossary),
            ("data/chapters.json", _missing_chapter_library),
        )
    ):
        built = _prepare(
            tmp_path / f"modified-{index}",
            monkeypatch=monkeypatch,
            mutations=((relative, mutate),),
        )
        # The modified evidence byte must fail closed at the FIRST guarded
        # boundary it reaches: the preflight itself re-derives the historical
        # incident, so the failure may land there rather than in the execute.
        with pytest.raises(g7o.G7ORecoveryError):
            authorization = _authorization(
                g7o.preflight_ch1_state_recovery(**_preflight_kwargs(built))
            )
            g7o.execute_ch1_state_recovery(
                **_preflight_kwargs(built),
                authorization=authorization,
                authorize_paid_run=True,
                secret_reader=reader,
                client_factory=factory,
            )
    assert calls == {"client_factory": 0, "secret_reader": 0}


# --------------------------------------------------------------------------
# 5. The consumed historical authorization is never reusable
# --------------------------------------------------------------------------


def test_consumed_historical_authorization_not_reusable(env, preflight):
    base = _authorization(preflight)
    normalized = g7o.validate_fresh_recovery_authorization(
        base, preflight=preflight
    )
    assert normalized["operator_authorized"] is True
    assert normalized["authorization_validated"] is True
    assert normalized["historical_consumed_authorization_reusable"] is False
    assert normalized["credential_access_before_authorization"] is False

    without_flag = {
        key: value
        for key, value in base.items()
        if key != "operator_authorized"
    }
    with pytest.raises(g7o.G7ORecoveryError, match="operator"):
        g7o.validate_fresh_recovery_authorization(
            without_flag, preflight=preflight
        )

    for field, bad in (
        ("recovery_generation_id", "G7O_CH1_STATE_RECOVERY_OTHER"),
        ("recovery_binding_sha256", "0" * 64),
        ("run_id", "runs/other_run"),
        ("novel_id", "other_novel"),
        ("chapter", 2),
        ("model_id", "z-ai/glm-5.3-flash"),
        ("corrected_auxiliary_budget", 3_000),
        ("maximum_new_provider_sends", 3),
        ("maximum_credit_equivalent_usd", "0.5"),
    ):
        with pytest.raises(g7o.G7ORecoveryError, match=field):
            g7o.validate_fresh_recovery_authorization(
                {**base, field: bad}, preflight=preflight
            )

    # The consumed executor authorization must never be an active grant.
    for smuggled in (
        {
            "active_authorization": {
                "plan_sha256": g7n.ORIGINAL_LEDGER_PLAN_SHA256,
                "approval_token": g7n.ORIGINAL_LEDGER_APPROVAL_TOKEN,
            }
        },
        {"plan_sha256": g7n.ORIGINAL_LEDGER_PLAN_SHA256},
        {"approval_token": g7n.ORIGINAL_LEDGER_APPROVAL_TOKEN},
        {"reuse_plan_sha256": g7n.ORIGINAL_LEDGER_PLAN_SHA256},
    ):
        with pytest.raises(g7o.G7ORecoveryError, match="consumed"):
            g7o.validate_fresh_recovery_authorization(
                {**base, **smuggled}, preflight=preflight
            )

    # A NON-active historical note is tolerated.
    note = {
        **base,
        "historical_consumed_plan_sha256": g7n.ORIGINAL_LEDGER_PLAN_SHA256,
        "historical_consumed_approval_token": g7n.ORIGINAL_LEDGER_APPROVAL_TOKEN,
    }
    assert g7o.validate_fresh_recovery_authorization(
        note, preflight=preflight
    )["historical_consumed_authorization_reusable"] is False

    # No extra paid operation may be smuggled into the authorization.
    with pytest.raises(g7o.G7ORecoveryError, match="state extraction"):
        g7o.validate_fresh_recovery_authorization(
            {**base, "logical_operations": ["glossary_review"]},
            preflight=preflight,
        )
    assert g7o.validate_fresh_recovery_authorization(
        {**base, "logical_operations": ["state_extraction"]},
        preflight=preflight,
    )["operator_authorized"] is True


# --------------------------------------------------------------------------
# 6/7. No Chapter-1 translation path, no Chapter-2 path
# --------------------------------------------------------------------------


def test_no_chapter1_translation_path_exists(env, preflight):
    g7o.assert_no_translation_or_chapter2_path()
    source = Path(g7o.__file__).read_text(encoding="utf-8")
    for symbol in g7o._FORBIDDEN_PATH_SYMBOLS:
        assert symbol not in source
    assert "run_unattended" + "_production" not in source
    assert g7o.ALLOWED_RECOVERY_OPERATIONS == frozenset({"state_extraction"})
    assert config.LIFECYCLE_OPERATION_TRANSLATION not in (
        g7o.ALLOWED_RECOVERY_OPERATIONS
    )
    assert g7o.RECOVERY_LOGICAL_OPERATION == (
        config.LIFECYCLE_OPERATION_STATE_EXTRACTION
    )

    # A translation-operation request can never be constructed.
    with pytest.raises(g7o.G7ORecoveryError, match="recovery envelope"):
        g7o._recovery_request_body(
            preflight=preflight,
            logical_operation=config.LIFECYCLE_OPERATION_TRANSLATION,
            messages=[],
        )

    # Every durable send stays inside the recovery envelope.
    outcome, _client = _run_success(
        env, preflight, first_content="{malformed state json"
    )
    ledger = json.loads(_ledger_path(env).read_text(encoding="utf-8"))
    assert [
        record["logical_operation"] for record in ledger["records"]
    ] == ["state_extraction", "state_json_repair"]
    for record in ledger["records"]:
        assert record["logical_operation"] != (
            config.LIFECYCLE_OPERATION_TRANSLATION
        )
        assert record["chapter"] == 1
    assert outcome["logical_operation"] == "state_extraction"
    assert len(outcome["journal_kinds"]) == 2
    assert outcome["journal_kinds"][0] == g7o.RECOVERY_JOURNAL_KIND
    assert outcome["journal_kinds"][1] != outcome["journal_kinds"][0]
    assert outcome["journal_kinds"][1].startswith(
        g7o.RECOVERY_JOURNAL_KIND + "_"
    )
    assert not any(
        "translation" in path.name for path in _journal_dir(env).iterdir()
    )
    assert g7o.RECOVERY_JOURNAL_KIND not in (
        config.PURE_RUN_LIFECYCLE_OPERATIONS
    )


def test_no_chapter2_path_exists(env, preflight):
    assert g7o.TARGET_CHAPTER == 1
    assert g7o.ALLOWED_RECOVERY_CHAPTERS == (1,)
    source = Path(g7o.__file__).read_text(encoding="utf-8")
    assert "chapter" + " = 2" not in source
    assert "next_chapter" + " = " not in source

    with pytest.raises(g7o.G7ORecoveryError, match="Chapter 1"):
        g7o._validated_preflight({**preflight, "chapter": 2})
    with pytest.raises(g7o.G7ORecoveryError, match="Chapter 1"):
        g7o._recovery_request_body(
            preflight={**preflight, "chapter": 2},
            logical_operation="state_extraction",
            messages=[],
        )

    progress_before = (env.run_root / "data" / "progress.json").read_bytes()
    result, _client = _execute_success(env, preflight)
    assert result["status"] == g7o.RECOVERY_APPLIED_STATUS
    assert result["chapter"] == 1
    assert result["chapter_2_execution"] is False
    assert result["applied_state"]["chapter_2_advanced"] is False
    progress = json.loads(
        (env.run_root / "data" / "progress.json").read_text(encoding="utf-8")
    )
    assert progress["completed"] == [1]
    assert progress["last_completed"] == 1
    assert progress["next_chapter"] == 2
    assert (env.run_root / "data" / "progress.json").read_bytes() == (
        progress_before
    )


# --------------------------------------------------------------------------
# 8/9. Journal and ledger namespace separation
# --------------------------------------------------------------------------


def test_fixed_historical_state_journal_slots_not_reused(env, preflight):
    journal_dir = _journal_dir(env)
    historical = {
        path.name: path.read_bytes() for path in journal_dir.iterdir()
    }
    assert set(historical) == {
        "0001.g7c_state_extraction.json",
        "0001.g7c_state_json_repair.json",
        "0001.g7l_ch1_state_recovery_repair.json",
    }

    recovery_path = g7o.recovery_journal_path(journal_dir)
    assert recovery_path.name == "0001.g7n_ch1_state_recovery_8k_repair.json"
    assert recovery_path.name != (
        f"0001.{g7n.FIXED_STATE_EXTRACTION_JOURNAL_KIND}.json"
    )
    assert recovery_path.name != (
        f"0001.{g7n.FIXED_STATE_JSON_REPAIR_JOURNAL_KIND}.json"
    )
    assert not recovery_path.name.startswith(f"0001.{g7n.G7C_JOURNAL_KIND_PREFIX}")

    outcome, client = _run_success(
        env, preflight, first_content="{malformed state json"
    )
    assert len(client.calls) == 2

    names = {path.name for path in journal_dir.iterdir()}
    new_names = names - set(historical)
    assert len(new_names) == 2
    for name in new_names:
        assert name.startswith("0001.g7n_ch1_state_recovery_8k_repair")
        assert not name.startswith(f"0001.{g7n.G7C_JOURNAL_KIND_PREFIX}")
    assert outcome["journal_kinds"][0] == g7o.RECOVERY_JOURNAL_KIND
    assert outcome["journal_kinds"][1] != g7o.RECOVERY_JOURNAL_KIND
    assert outcome["journal_kinds"][1].startswith(
        g7o.RECOVERY_JOURNAL_KIND + "_"
    )
    repair_record = json.loads(
        Path(outcome["journal_path"]).read_text(encoding="utf-8")
    )
    assert repair_record["operation_kind"] == outcome["journal_kinds"][1]
    assert repair_record["operation_kind"] != (
        g7n.FIXED_STATE_JSON_REPAIR_JOURNAL_KIND
    )
    assert repair_record["request_sha256"] == _sha256_json(client.calls[1])
    assert repair_record["translation_sha256"] == _sha256_text(
        "{malformed state json"
    )

    for name, payload in historical.items():
        assert (journal_dir / name).read_bytes() == payload


def test_separate_recovery_journal_and_ledger_namespace(env, preflight):
    assert g7o.RECOVERY_JOURNAL_KIND == g7n.RECOVERY_OPERATION_ID
    assert g7o.RECOVERY_JOURNAL_KIND == "g7n_ch1_state_recovery_8k_repair"
    assert g7o.RECOVERY_LEDGER_DIR_RELATIVE == "data/g7o_recovery_ledgers"
    assert g7o.RECOVERY_LEDGER_RELATIVE == "data/g7o_recovery_ledgers/0001.json"
    assert g7o.RECOVERY_LEDGER_REVISION == (
        "g7o-ch1-state-recovery-ledger-v1"
    )
    assert g7o.RECOVERY_LEDGER_SCHEMA_VERSION == 1

    ledger_path = g7o.recovery_ledger_path(env.run_root)
    assert ledger_path == env.run_root / "data" / "g7o_recovery_ledgers" / (
        "0001.json"
    )
    historical_ledger = env.run_root / g7n.LEDGER_RELATIVE
    assert ledger_path != historical_ledger
    assert g7n.LEDGER_RELATIVE == "data/g7f_invocation_ledgers/0001.json"
    before = historical_ledger.read_bytes()

    _run_success(env, preflight)
    assert historical_ledger.read_bytes() == before

    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert ledger["schema_version"] == 1
    assert ledger["revision"] == "g7o-ch1-state-recovery-ledger-v1"
    assert ledger["recovery_generation_id"] == (
        preflight["recovery_generation_id"]
    )
    assert ledger["recovery_binding_sha256"] == (
        preflight["recovery_binding_sha256"]
    )
    assert ledger["plan_sha256"] == preflight["plan_sha256"]
    assert ledger["model_id"] == MODEL_ID
    assert ledger["novel_id"] == NOVEL_ID
    assert ledger["target_chapter"] == 1
    assert ledger["logical_operation"] == "state_extraction"
    assert ledger["maximum_new_provider_sends"] == 2
    assert ledger["maximum_credit_equivalent_usd"] == "0.05"
    assert ledger["new_provider_sends_started"] == 1
    assert isinstance(ledger["records"], list) and len(ledger["records"]) == 1
    assert ledger["records"][0]["status"] == "accepted"
    assert set(ledger) == {
        "schema_version",
        "revision",
        "logical_operation",
        "recovery_generation_id",
        "recovery_binding_sha256",
        "plan_sha256",
        "run_id",
        "novel_id",
        "model_id",
        "target_chapter",
        "maximum_new_provider_sends",
        "maximum_credit_equivalent_usd",
        "new_provider_sends_started",
        "accepted_credit_equivalent_usd",
        "records",
        "created_utc",
        "updated_utc",
        "ledger_sha256",
    }
    assert ledger["ledger_sha256"] == _sha256_json(
        {key: item for key, item in ledger.items() if key != "ledger_sha256"}
    )
    assert not (
        env.run_root / "data" / "g7f_invocation_ledgers" / "0002.json"
    ).exists()


# --------------------------------------------------------------------------
# 10. Send and credit-equivalent ceilings
# --------------------------------------------------------------------------


def test_send_and_ce_ceilings_enforced(env, preflight, tmp_path):
    assert preflight["maximum_new_provider_sends"] == g7o.MAX_NEW_SENDS == 2
    assert preflight["maximum_credit_equivalent_usd"] == g7o.MAX_CE_USD == "0.05"
    assert g7o.RECOVERY_MAX_COMPLETION_TOKENS == 8_000

    body = g7o._recovery_request_body(
        preflight=preflight,
        logical_operation="state_extraction",
        messages=g7o._state_extraction_messages(preflight),
    )
    ledger = g7o.RecoveryBudgetLedger.open_or_create(
        path=tmp_path / "ledger-a.json",
        preflight=preflight,
        clock=_clock,
    )
    preview = ledger.preflight_next_send(
        request_body=body, request_started_utc=FIXED_NOW
    )
    assert Decimal(preview["conservative_cost_bound_usd"]) <= Decimal("0.05")
    assert Decimal(preview["remaining_credit_equivalent_usd"]) == Decimal(
        "0.05"
    )

    ledger.begin_send(
        logical_operation="state_extraction",
        chapter=1,
        request_sha256="a" * 64,
        request_body=body,
        request_started_utc=FIXED_NOW,
    )
    ledger.begin_send(
        logical_operation=config.LIFECYCLE_OPERATION_STATE_JSON_REPAIR,
        chapter=1,
        request_sha256="b" * 64,
        request_body=body,
        request_started_utc=FIXED_NOW,
    )
    assert ledger.new_provider_sends_started == 2
    with pytest.raises(g7o.G7ORecoveryError, match="ceiling is reached"):
        ledger.begin_send(
            logical_operation="state_extraction",
            chapter=1,
            request_sha256="c" * 64,
            request_body=body,
            request_started_utc=FIXED_NOW,
        )

    # A send outside the recovery envelope is structurally impossible.
    with pytest.raises(g7o.G7ORecoveryError, match="recovery envelope"):
        ledger.begin_send(
            logical_operation=config.LIFECYCLE_OPERATION_GLOSSARY_REVIEW,
            chapter=1,
            request_sha256="d" * 64,
            request_body=body,
            request_started_utc=FIXED_NOW,
        )

    # Accepted usage above the credit-equivalent ceiling fails closed.
    over = g7o.RecoveryBudgetLedger.open_or_create(
        path=tmp_path / "ledger-b.json",
        preflight=preflight,
        clock=_clock,
    )
    over.begin_send(
        logical_operation="state_extraction",
        chapter=1,
        request_sha256="e" * 64,
        request_body=body,
        request_started_utc=FIXED_NOW,
    )
    with pytest.raises(g7o.G7ORecoveryError, match="credit-equivalent ceiling"):
        over.record_accepted_usage(
            send_index=1,
            request_sha256="e" * 64,
            usage=SimpleNamespace(
                prompt_tokens=0,
                completion_tokens=10_000_000,
                prompt_tokens_details=None,
            ),
            request_started_utc=FIXED_NOW,
        )
    assert over.accepted_cost > Decimal("0.05")
    with pytest.raises(g7o.G7ORecoveryError):
        over.preflight_next_send(request_body=body, request_started_utc=FIXED_NOW)


# --------------------------------------------------------------------------
# 11. The corrected 8,000 ceiling reaches the request identity pre-transport
# --------------------------------------------------------------------------


def test_corrected_8000_budget_reaches_request_identity_before_transport(
    env, preflight
):
    journal_path = g7o.recovery_journal_path(_journal_dir(env))
    seen = {}

    def on_call(kwargs):
        # At transport time the durable journal slot must already be `started`
        # and must already bind the EXACT 8,000-token request body.
        record = json.loads(journal_path.read_text(encoding="utf-8"))
        assert record["status"] == "started"
        assert record["request_sha256"] == _sha256_json(kwargs)
        assert kwargs["max_completion_tokens"] == 8_000
        assert kwargs["reasoning_effort"] == g7o.TARGET_REASONING_EFFORT
        assert kwargs["model"] == MODEL_ID
        seen.update(kwargs)

    outcome, client = _run(
        env,
        preflight,
        [_provider_response(_canonical(RECOVERED_STATE))],
        on_call=on_call,
    )
    assert seen == client.calls[0]
    assert outcome["status"] == g7o.RECOVERY_ACCEPTED_STATUS
    assert outcome["max_completion_tokens"] == 8_000

    record = json.loads(journal_path.read_text(encoding="utf-8"))
    assert record["request_sha256"] == _sha256_json(client.calls[0])
    ledger = json.loads(_ledger_path(env).read_text(encoding="utf-8"))
    assert ledger["records"][0]["request_sha256"] == record["request_sha256"]
    assert ledger["records"][0]["stats"]["completion_tokens"] == 120

    # The corrected ceiling is DERIVED from the reused G7J GOAT auxiliary policy.
    assert g7o._corrected_auxiliary_completion_budget(
        logical_operation="state_extraction",
        requested_tokens=g7j.GOAT_AUXILIARY_LEGACY_BASELINE_COMPLETION_TOKENS,
    ) == 8_000
    assert g7o._corrected_auxiliary_completion_budget(
        logical_operation="state_extraction", requested_tokens=8_000
    ) == 8_000
    assert g7j.goat_auxiliary_8k_completion_budget(
        logical_operation="state_extraction",
        requested_tokens=g7j.GOAT_AUXILIARY_LEGACY_BASELINE_COMPLETION_TOKENS,
    ) == g7o.RECOVERY_MAX_COMPLETION_TOKENS
    for drifted in (32_000, 5_000, 3_001):
        with pytest.raises(g7o.G7ORecoveryError):
            g7o._corrected_auxiliary_completion_budget(
                logical_operation="state_extraction",
                requested_tokens=drifted,
            )

    # The user prompt embeds the exact committed recovery inputs.
    user_prompt = client.calls[0]["messages"][1]["content"]
    assert "CHAPTER: 1" in user_prompt
    assert "EXISTING GLOSSARY TERMS: []" in user_prompt
    assert CHAPTER_SOURCE_TEXT in user_prompt
    assert COMMITTED_TRANSLATION_TEXT in user_prompt
    system_prompt = client.calls[0]["messages"][0]["content"]
    assert '"new_terms"' in system_prompt
    assert '"continuity_summary"' in system_prompt
    assert '"issues"' in system_prompt


# --------------------------------------------------------------------------
# 12. Empty / length-terminated responses fail closed without repair
# --------------------------------------------------------------------------


def test_empty_or_length_recovery_response_fails_closed_without_repair(
    tmp_path, monkeypatch
):
    cases = (
        (
            "budget-exhausted",
            "length",
            8_000,
            8_000,
            g7j.AUXILIARY_BUDGET_EXHAUSTED_EMPTY_CONTENT,
        ),
        (
            "length-below-ceiling",
            "length",
            10,
            None,
            g7j.AUXILIARY_EMPTY_CONTENT_LENGTH,
        ),
        (
            "anomalous-finish",
            "stop",
            0,
            None,
            g7j.AUXILIARY_EMPTY_CONTENT_ANOMALOUS_FINISH,
        ),
    )
    assert g7o.RECOVERY_MAX_COMPLETION_TOKENS == 8_000

    for tag, finish, completion, reasoning, expected in cases:
        built = _prepare(tmp_path / tag, monkeypatch=monkeypatch)
        state = g7o.preflight_ch1_state_recovery(**_preflight_kwargs(built))
        before = _state_bytes(built)
        outcome, client = _run(
            built,
            state,
            [
                _provider_response(
                    "",
                    finish_reason=finish,
                    completion_tokens=completion,
                    reasoning_tokens=reasoning,
                )
            ],
        )
        assert outcome["status"] == g7o.RECOVERY_FAIL_CLOSED_STATUS
        assert outcome["classification"] == expected
        assert outcome["fail_closed"] is True
        assert outcome["recovered_state"] is None
        assert outcome["state_source"] is None
        assert outcome["repair_send_used"] is False
        assert outcome["apply_performed"] is False
        assert outcome["provider_calls"] == 1
        assert outcome["new_provider_sends_started"] == 1
        assert len(client.calls) == 1
        assert "no JSON repair" in outcome["blocker"]

        names = {path.name for path in _journal_dir(built).iterdir()}
        assert names == {
            "0001.g7c_state_extraction.json",
            "0001.g7c_state_json_repair.json",
            "0001.g7l_ch1_state_recovery_repair.json",
            "0001.g7n_ch1_state_recovery_8k_repair.json",
        }
        assert _state_bytes(built) == before

        ledger = json.loads(_ledger_path(built).read_text(encoding="utf-8"))
        assert ledger["records"][0]["status"] == "accepted"
        assert ledger["records"][0]["logical_operation"] == "state_extraction"


# --------------------------------------------------------------------------
# 13. Nonempty malformed state may use ONE bounded repair
# --------------------------------------------------------------------------


def test_malformed_nonempty_state_may_use_one_bounded_repair(
    tmp_path, monkeypatch
):
    built = _prepare(tmp_path / "repairable", monkeypatch=monkeypatch)
    state = g7o.preflight_ch1_state_recovery(**_preflight_kwargs(built))
    outcome, client = _run(
        built,
        state,
        [
            _provider_response("{malformed state json"),
            _provider_response(_canonical(RECOVERED_STATE)),
        ],
    )
    assert len(client.calls) == 2
    assert outcome["status"] == g7o.RECOVERY_ACCEPTED_STATUS
    assert outcome["repair_send_used"] is True
    assert outcome["initial_classification"] == (
        g7j.AUXILIARY_MALFORMED_JSON_REPAIRED
    )
    assert outcome["classification"] == g7j.AUXILIARY_STATE_ACCEPTED
    assert outcome["fail_closed"] is False
    assert outcome["provider_calls"] == 2
    assert outcome["new_provider_sends_started"] == 2
    assert outcome["state_source"] == "provider_response_bounded_repair"
    assert outcome["recovered_state"] == RECOVERED_STATE

    repair_prompt = client.calls[1]["messages"][1]["content"]
    assert "Repair this malformed JSON" in repair_prompt
    assert "{malformed state json" in repair_prompt
    repair_prefix = f"0001.{g7o.RECOVERY_JOURNAL_KIND}_"
    repair_names = [
        path.name
        for path in _journal_dir(built).iterdir()
        if path.name.startswith(repair_prefix)
    ]
    assert len(repair_names) == 1
    repair_record = json.loads(
        (_journal_dir(built) / repair_names[0]).read_text(encoding="utf-8")
    )
    assert repair_record["operation_kind"] == repair_names[0][
        len("0001.") : -len(".json")
    ]
    assert repair_record["translation_sha256"] not in ("", SHA256_EMPTY)
    assert repair_record["translation_sha256"] == _sha256_text(
        "{malformed state json"
    )
    assert repair_record["status"] == "response_recorded"
    assert repair_record["request_sha256"] == _sha256_json(client.calls[1])

    # A repair that is itself fail-closed stops after exactly two sends.
    failing = _prepare(tmp_path / "repair-fails", monkeypatch=monkeypatch)
    failing_state = g7o.preflight_ch1_state_recovery(
        **_preflight_kwargs(failing)
    )
    before = _state_bytes(failing)
    failed, failing_client = _run(
        failing,
        failing_state,
        [
            _provider_response("{malformed state json"),
            _provider_response("", finish_reason="length", completion_tokens=8_000),
        ],
    )
    assert len(failing_client.calls) == 2
    assert failed["status"] == g7o.RECOVERY_FAIL_CLOSED_STATUS
    assert failed["recovered_state"] is None
    assert failed["repair_send_used"] is True
    assert failed["fail_closed"] is True
    assert failed["provider_calls"] == 2
    assert _state_bytes(failing) == before

    # A JSON object that is not the required state may use the bounded repair too.
    invalid = _prepare(tmp_path / "schema-invalid", monkeypatch=monkeypatch)
    invalid_state = g7o.preflight_ch1_state_recovery(
        **_preflight_kwargs(invalid)
    )
    repaired, repaired_client = _run(
        invalid,
        invalid_state,
        [
            _provider_response(_canonical({"new_terms": "not-a-list"})),
            _provider_response(_canonical(RECOVERED_STATE)),
        ],
    )
    assert len(repaired_client.calls) == 2
    assert repaired["status"] == g7o.RECOVERY_ACCEPTED_STATUS
    assert repaired["initial_classification"] == g7j.AUXILIARY_STATE_ACCEPTED
    assert "new_terms" in repaired["initial_candidate_problem"]

    # A genuinely malformed repaired payload fails closed with no third send.
    hopeless = _prepare(tmp_path / "hopeless", monkeypatch=monkeypatch)
    hopeless_state = g7o.preflight_ch1_state_recovery(
        **_preflight_kwargs(hopeless)
    )
    hopeless_outcome, hopeless_client = _run(
        hopeless,
        hopeless_state,
        [
            _provider_response("{malformed state json"),
            _provider_response("{still malformed"),
        ],
    )
    assert len(hopeless_client.calls) == 2
    assert hopeless_outcome["status"] == g7o.RECOVERY_FAIL_CLOSED_STATUS
    assert hopeless_outcome["recovered_state"] is None
    assert hopeless_outcome["provider_calls"] == 2


# --------------------------------------------------------------------------
# 14. No automatic retry after a started or uncertain send
# --------------------------------------------------------------------------


def test_no_automatic_retry_after_started_or_uncertain(
    tmp_path, monkeypatch
):
    built = _prepare(tmp_path / "uncertain", monkeypatch=monkeypatch)
    state = g7o.preflight_ch1_state_recovery(**_preflight_kwargs(built))
    journal_path = g7o.recovery_journal_path(_journal_dir(built))

    first_client = _fake_client([RuntimeError("transport failed")])
    with pytest.raises(RuntimeError, match="transport failed"):
        g7o.run_recovery_request(
            preflight=state,
            recovery_ledger_path=_ledger_path(built),
            journal_dir=_journal_dir(built),
            provider_client=first_client,
            clock=_clock,
        )
    assert len(first_client.calls) == 1
    record = json.loads(journal_path.read_text(encoding="utf-8"))
    assert record["status"] == "uncertain"
    ledger = json.loads(_ledger_path(built).read_text(encoding="utf-8"))
    assert ledger["records"][0]["status"] == "transport_exception"
    assert ledger["new_provider_sends_started"] == 1

    retry_client = _fake_client([_provider_response(_canonical(RECOVERED_STATE))])
    with pytest.raises(PaidRequestUncertainError):
        g7o.run_recovery_request(
            preflight=state,
            recovery_ledger_path=_ledger_path(built),
            journal_dir=_journal_dir(built),
            provider_client=retry_client,
            clock=_clock,
        )
    assert retry_client.calls == []
    after = json.loads(journal_path.read_text(encoding="utf-8"))
    assert after["status"] == "uncertain"
    assert after["record_sha256"] == record["record_sha256"]

    # A process death after `started` blocks automatic reissue as well.
    started = _prepare(tmp_path / "started", monkeypatch=monkeypatch)
    started_state = g7o.preflight_ch1_state_recovery(
        **_preflight_kwargs(started)
    )
    request_body = g7o._recovery_request_body(
        preflight=started_state,
        logical_operation="state_extraction",
        messages=g7o._state_extraction_messages(started_state),
    )
    identity = request_identity(
        operation_kind=g7o.RECOVERY_JOURNAL_KIND,
        novel_id=started_state["novel_id"],
        chapter=1,
        provider="command_code_provider",
        model=request_body["model"],
        profile_id=started_state["profile_id"],
        source_sha256=started_state["source_chapter_sha256"],
        translation_sha256=started_state["committed_translation_sha256"],
        request_payload=request_body,
    )
    started_record = {
        "schema_version": 1,
        "revision": "paid-request-journal-v1",
        **identity,
        "status": "started",
        "attempt_context": {"mode": "process-death-after-started"},
        "response": {},
        "response_sha256": "",
        "error": {},
        "created_utc": "2026-09-23T02:30:00+00:00",
        "updated_utc": "2026-09-23T02:30:01+00:00",
    }
    started_record["record_sha256"] = _sha256_json(started_record)
    started_path = paid_request_journal_path(
        _journal_dir(started), g7o.RECOVERY_JOURNAL_KIND, 1
    )
    _write_json(started_path, started_record)

    blocked_client = _fake_client([_provider_response(_canonical(RECOVERED_STATE))])
    with pytest.raises(PaidRequestUncertainError):
        g7o.run_recovery_request(
            preflight=started_state,
            recovery_ledger_path=_ledger_path(started),
            journal_dir=_journal_dir(started),
            provider_client=blocked_client,
            clock=_clock,
        )
    assert blocked_client.calls == []
    assert json.loads(started_path.read_text(encoding="utf-8")) == (
        started_record
    )


# --------------------------------------------------------------------------
# 15/16. Historical journals, ledger, Chapter-1 output and QA stay identical
# --------------------------------------------------------------------------


def test_original_journals_and_ledger_byte_identical(env, preflight):
    protected = {
        "extraction": env.extraction_journal.read_bytes(),
        "repair": env.repair_journal.read_bytes(),
        "ledger": env.historical_ledger.read_bytes(),
    }
    result, client = _execute_success(
        env, preflight, first_content="{malformed state json"
    )
    assert len(client.calls) == 2
    assert result["status"] == g7o.RECOVERY_APPLIED_STATUS

    assert env.extraction_journal.read_bytes() == protected["extraction"]
    assert env.repair_journal.read_bytes() == protected["repair"]
    assert env.historical_ledger.read_bytes() == protected["ledger"]
    assert preflight["original_journals"]["state_extraction"][
        "file_sha256"
    ] == hashlib.sha256(protected["extraction"]).hexdigest()
    assert preflight["original_journals"]["state_json_repair"][
        "file_sha256"
    ] == hashlib.sha256(protected["repair"]).hexdigest()
    assert preflight["original_ledger"]["file_sha256"] == hashlib.sha256(
        protected["ledger"]
    ).hexdigest()


def test_chapter1_output_and_qa_byte_identical(env, preflight):
    chapter_bytes = env.committed.read_bytes()
    qa_bytes = env.qa.read_bytes()

    result, _client = _execute_success(env, preflight)
    assert result["status"] == g7o.RECOVERY_APPLIED_STATUS
    assert env.committed.read_bytes() == chapter_bytes
    assert env.qa.read_bytes() == qa_bytes
    assert hashlib.sha256(chapter_bytes).hexdigest() == (
        preflight["committed_translation_sha256"]
    )
    assert hashlib.sha256(qa_bytes).hexdigest() == preflight["qa_sha256"]
    assert result["applied_state"]["apply"]["committed_translation_sha256"] == (
        preflight["committed_translation_sha256"]
    )
    assert result["applied_state"]["apply"]["qa_sha256"] == (
        preflight["qa_sha256"]
    )
    assert result["chapter_1_retranslated"] is False
    assert result["chapter_1_translation_retry"] is False

    # Retransmitting Chapter 1 is structurally impossible: no send may ever be a
    # translation operation, and the committed output bytes are never rewritten.
    ledger = json.loads(_ledger_path(env).read_text(encoding="utf-8"))
    assert all(
        record["logical_operation"] != config.LIFECYCLE_OPERATION_TRANSLATION
        for record in ledger["records"]
    )
    assert env.committed.read_bytes() == chapter_bytes


# --------------------------------------------------------------------------
# 17. Atomic apply and rollback
# --------------------------------------------------------------------------


def test_atomic_state_apply_and_rollback(env, preflight, monkeypatch):
    before = _state_bytes(env)
    run_root = Path(preflight["run_root"])

    # (a) A malformed candidate never mutates anything.
    for bad in (
        "not-a-mapping",
        {"new_terms": "nope", "continuity_summary": "s", "issues": []},
        {"new_terms": [], "continuity_summary": 7, "issues": []},
        {"new_terms": [], "continuity_summary": "s", "issues": "nope"},
        {
            "new_terms": [{"chinese": "", "english": "e", "category": "c"}],
            "continuity_summary": "s",
            "issues": [],
        },
        {
            "new_terms": [{"chinese": "a", "english": "e", "category": "c",
                           "status": "locked"}],
            "continuity_summary": "s",
            "issues": [],
        },
        {
            "new_terms": [
                {"chinese": "a", "english": "e", "category": "c"},
                {"chinese": "a", "english": "e2", "category": "c"},
            ],
            "continuity_summary": "s",
            "issues": [],
        },
    ):
        with pytest.raises(g7o.G7ORecoveryError):
            g7o.apply_recovered_state(
                run_root=run_root, preflight=preflight, recovered_state=bad
            )
    assert _state_bytes(env) == before

    # (b) A failure part-way through the transaction restores the prior bytes.
    real_write = g7o.atomic_write_json
    writes = {"count": 0}

    def flaky_write(path, document):
        writes["count"] += 1
        if writes["count"] == 3:
            raise OSError("simulated disk failure")
        return real_write(path, document)

    monkeypatch.setattr(g7o, "atomic_write_json", flaky_write)
    with pytest.raises(g7o.G7ORecoveryError):
        g7o.apply_recovered_state(
            run_root=run_root,
            preflight=preflight,
            recovered_state=RECOVERED_STATE,
        )
    assert writes["count"] == 3
    assert _state_bytes(env) == before
    monkeypatch.setattr(g7o, "atomic_write_json", real_write)

    # (c) The happy path writes exactly the engine's on-disk shapes.
    applied = g7o.apply_recovered_state(
        run_root=run_root,
        preflight=preflight,
        recovered_state=RECOVERED_STATE,
    )
    assert applied["status"] == g7o.RECOVERY_APPLIED_STATUS
    assert applied["new_terms_committed"] == 2
    assert applied["glossary_review_performed"] is False

    glossary = json.loads(
        (env.run_root / "data" / "glossary.json").read_text(encoding="utf-8")
    )
    assert glossary == {
        "entries": {
            "第一章": {
                "english": "Chapter One",
                "category": "specialized recurring term",
                "status": "provisional",
                "first_chapter": 1,
                "notes": "opening chapter marker",
            },
            "测试": {
                "english": "Test",
                "category": "specialized recurring term",
                "status": "provisional",
                "first_chapter": 1,
                "notes": "",
            },
        }
    }
    continuity = json.loads(
        (env.run_root / "data" / "continuity.json").read_text(encoding="utf-8")
    )
    assert continuity == {
        "latest_chapter": 1,
        "summary": RECOVERED_STATE["continuity_summary"],
    }
    issues = json.loads(
        (env.run_root / "data" / "issues.json").read_text(encoding="utf-8")
    )
    assert issues == {"issues": []}

    # Applying to another run root is refused.
    with pytest.raises(g7o.G7ORecoveryError, match="preflighted run root"):
        g7o.apply_recovered_state(
            run_root=env.run_root.parent,
            preflight=preflight,
            recovered_state=RECOVERED_STATE,
        )


# --------------------------------------------------------------------------
# 18. Progress stays Chapter 1 complete / Chapter 2 next
# --------------------------------------------------------------------------


def test_progress_stays_one_to_two(env, preflight, tmp_path, monkeypatch):
    progress_path = env.run_root / "data" / "progress.json"
    before = progress_path.read_bytes()
    assert json.loads(before.decode("utf-8"))["next_chapter"] == 2

    applied = g7o.apply_recovered_state(
        run_root=Path(preflight["run_root"]),
        preflight=preflight,
        recovered_state=RECOVERED_STATE,
    )
    assert applied["progress_unchanged"] is True
    assert progress_path.read_bytes() == before
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    assert progress["completed"] == [1]
    assert progress["last_completed"] == 1
    assert progress["next_chapter"] == 2
    assert applied["chapter_2_advanced"] is False
    assert preflight["progress"] == {
        "completed": [1],
        "last_completed": 1,
        "next_chapter": 2,
    }

    # An advanced progress file is refused by the apply re-verification, and
    # nothing is mutated.
    def _advance(path):
        document = json.loads(path.read_text(encoding="utf-8"))
        document["completed"] = [1, 2]
        document["last_completed"] = 2
        document["next_chapter"] = 3
        path.write_text(_canonical(document), encoding="utf-8")

    advanced = _prepare(tmp_path / "advanced", monkeypatch=monkeypatch)
    advanced_preflight = g7o.preflight_ch1_state_recovery(
        **_preflight_kwargs(advanced)
    )
    state_files_before = _state_bytes(advanced)
    _advance(advanced.run_root / "data" / "progress.json")
    with pytest.raises(g7o.G7ORecoveryError):
        g7o.apply_recovered_state(
            run_root=Path(advanced_preflight["run_root"]),
            preflight=advanced_preflight,
            recovered_state=RECOVERED_STATE,
        )
    assert _state_bytes(advanced) == state_files_before


# --------------------------------------------------------------------------
# 19. Zero provider call during preflight, planning and tests
# --------------------------------------------------------------------------


def test_no_provider_call_during_preflight_planning_and_tests(env, monkeypatch):
    calls, factory, reader = _guards(monkeypatch)
    monkeypatch.setattr(os, "getenv", factory)
    evidence_before = _snapshot(env.evidence_root)
    run_before = _snapshot(env.run_root)

    assert g7o.assert_no_translation_or_chapter2_path() is None
    plan = g7n.plan_ch1_state_recovery(env.evidence_root, env.run_id)
    assert plan["provider_calls"] == 0
    report = g7n.verify_recovery_plan(env.plan_path)
    assert report["provider_calls"] == 0
    assert report["credential_access"] is False
    preflight = g7o.preflight_ch1_state_recovery(**_preflight_kwargs(env))
    authorization = g7o.validate_fresh_recovery_authorization(
        _authorization(preflight), preflight=preflight
    )
    assert authorization["provider_calls"] == 0
    assert "PROVIDER_CLIENT_CONSTRUCTED=NO" in g7o.recovery_summary(preflight)

    assert calls == {"client_factory": 0, "secret_reader": 0}
    assert _snapshot(env.evidence_root) == evidence_before
    assert _snapshot(env.run_root) == run_before

    # A non-authorized execute touches neither the credential nor a client.
    with pytest.raises(g7o.G7ORecoveryError, match="authorize_paid_run"):
        g7o.execute_ch1_state_recovery(
            **_preflight_kwargs(env),
            authorization=_authorization(preflight),
            authorize_paid_run=False,
            secret_reader=reader,
            client_factory=factory,
        )
    # A failed fresh authorization also stops before any credential access.
    with pytest.raises(g7o.G7ORecoveryError):
        g7o.execute_ch1_state_recovery(
            **_preflight_kwargs(env),
            authorization={"operator_authorized": True},
            authorize_paid_run=True,
            secret_reader=reader,
            client_factory=factory,
        )
    assert calls == {"client_factory": 0, "secret_reader": 0}
    assert _snapshot(env.run_root) == run_before


# --------------------------------------------------------------------------
# 20. PLAN_REQUIRES_REMATERIALIZATION flag
# --------------------------------------------------------------------------


def test_plan_requires_rematerialization_flag(env, preflight, tmp_path, monkeypatch):
    assert preflight["plan_requires_rematerialization"] is False
    assert "PLAN_REQUIRES_REMATERIALIZATION=NO" in g7o.recovery_summary(preflight)
    assert g7o._plan_requires_rematerialization(env.plan) is False
    assert g7o._plan_requires_rematerialization(
        {**env.plan, "plan_requires_rematerialization": True}
    ) is False

    def _block_glossary_review(value):
        for step in value["post_recovery_pipeline"]["steps"]:
            if step.get("step") == "normal_chapter_1_glossary_review":
                step["blocked_by_this_plan"] = True

    def _drop_glossary_review(value):
        value["post_recovery_pipeline"]["steps"] = [
            step
            for step in value["post_recovery_pipeline"]["steps"]
            if step.get("step") != "normal_chapter_1_glossary_review"
        ]

    def _smuggle_extra_operation(value):
        value["operations"][0]["logical_operation"] = (
            config.LIFECYCLE_OPERATION_GLOSSARY_REVIEW
        )

    cases = (
        ("blocked-review", _block_glossary_review),
        ("dropped-review", _drop_glossary_review),
        ("smuggled-operation", _smuggle_extra_operation),
    )
    calls, factory, reader = _guards(monkeypatch)
    for index, (name, mutator) in enumerate(cases):
        tampered = _tamper(env.plan, mutator)
        assert g7o._plan_requires_rematerialization(tampered) is True
        path = _write_plan(env, tampered, name=f"{index}-{name}.json")
        with pytest.raises(
            g7o.G7ORecoveryError, match="PLAN_REQUIRES_REMATERIALIZATION"
        ):
            g7o.preflight_ch1_state_recovery(
                **_preflight_kwargs(env, plan=tampered, plan_path=path)
            )
        with pytest.raises(
            g7o.G7ORecoveryError, match="PLAN_REQUIRES_REMATERIALIZATION"
        ):
            g7o.execute_ch1_state_recovery(
                **_preflight_kwargs(env, plan=tampered, plan_path=path),
                authorization=_authorization(preflight),
                authorize_paid_run=True,
                secret_reader=reader,
                client_factory=factory,
            )
    assert calls == {"client_factory": 0, "secret_reader": 0}

    # A preflight object that claims rematerialization is refused outright.
    with pytest.raises(
        g7o.G7ORecoveryError, match="PLAN_REQUIRES_REMATERIALIZATION"
    ):
        g7o._validated_preflight(
            {**preflight, "plan_requires_rematerialization": True}
        )


# --------------------------------------------------------------------------
# 21. The real CRLF committed artifact keeps both byte identities
# --------------------------------------------------------------------------
#
# The historical committed Chapter-1 artifact is CRLF-terminated. The frozen
# raw-file SHA stays authoritative AND the exact text carried into the recovery
# request must re-encode to it, so the normalizing text read can never be used
# on this byte-identity bound artifact. Every expectation below is derived from
# the artifact bytes at hand; no forensic byte count or 8-hex digest is asserted.


def test_crlf_committed_artifact_passes_preflight_and_fresh_authorization(
    crlf_env,
):
    preflight = g7o.preflight_ch1_state_recovery(
        **_preflight_kwargs(crlf_env)
    )
    crlf_sha256 = hashlib.sha256(COMMITTED_TRANSLATION_CRLF_BYTES).hexdigest()

    assert preflight["status"] == "PREFLIGHT_OK"
    assert preflight["committed_translation_sha256"] == crlf_sha256
    text = preflight["committed_translation_text"]
    assert text.encode("utf-8") == COMMITTED_TRANSLATION_CRLF_BYTES
    assert "\r\n" in text
    assert hashlib.sha256(text.encode("utf-8")).hexdigest() == (
        preflight["committed_translation_sha256"]
    )

    validated = g7o.validate_fresh_recovery_authorization(
        _authorization(preflight), preflight=preflight
    )
    assert validated["operator_authorized"] is True
    assert validated["authorization_validated"] is True
    assert validated["provider_calls"] == 0
    assert validated["credential_access_before_authorization"] is False
    assert set(validated) >= set(g7o.RECOVERY_AUTHORIZATION_FIELDS)


def test_crlf_committed_text_reencodes_to_raw_file_sha_and_rejects_normalized_read(
    crlf_env,
):
    preflight = g7o.preflight_ch1_state_recovery(
        **_preflight_kwargs(crlf_env)
    )
    raw = crlf_env.committed.read_bytes()
    assert raw == COMMITTED_TRANSLATION_CRLF_BYTES
    raw_sha256 = hashlib.sha256(raw).hexdigest()

    # The frozen raw committed-file SHA is still verified against the file.
    assert raw_sha256 == preflight["committed_translation_sha256"]
    assert raw_sha256 == crlf_env.committed_sha256
    assert g7n.COMMITTED_TRANSLATION_SHA256.lower() == raw_sha256
    assert hashlib.sha256(
        preflight["committed_translation_text"].encode("utf-8")
    ).hexdigest() == raw_sha256

    # A universal-newline-normalized representation does NOT have that SHA, so
    # validation must never treat it as the same identity.
    normalized_sha256 = hashlib.sha256(
        raw.replace(b"\r\n", b"\n")
    ).hexdigest()
    assert normalized_sha256 != preflight["committed_translation_sha256"]
    assert normalized_sha256 == _sha256_text(
        preflight["committed_translation_text"].replace("\r\n", "\n")
    )


def test_crlf_committed_raw_byte_mutation_fails_closed_before_credential(
    tmp_path, monkeypatch
):
    def _rewrite_as_lf(path):
        path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n"))

    def _flip_one_byte(path):
        payload = bytearray(path.read_bytes())
        index = payload.index(b"\r")
        payload[index] = ord(";")
        path.write_bytes(bytes(payload))

    cases = (
        ("lf-rewrite", _rewrite_as_lf, False),
        ("byte-flip", _flip_one_byte, True),
    )
    calls, factory, reader = _guards(monkeypatch)
    for index, (tag, mutate, same_length) in enumerate(cases):
        built = _prepare_crlf(
            tmp_path / f"{index}-{tag}", monkeypatch=monkeypatch
        )
        before = built.committed.read_bytes()
        assert before == COMMITTED_TRANSLATION_CRLF_BYTES
        mutate(built.committed)
        after = built.committed.read_bytes()
        assert after != before
        assert (len(after) == len(before)) is same_length

        with pytest.raises(g7o.G7ORecoveryError):
            authorization = _authorization(
                g7o.preflight_ch1_state_recovery(**_preflight_kwargs(built))
            )
            g7o.execute_ch1_state_recovery(
                **_preflight_kwargs(built),
                authorization=authorization,
                authorize_paid_run=True,
                secret_reader=reader,
                client_factory=factory,
            )
    assert calls == {"client_factory": 0, "secret_reader": 0}


def test_crlf_committed_translation_text_mutation_fails_closed(crlf_env):
    preflight = g7o.preflight_ch1_state_recovery(
        **_preflight_kwargs(crlf_env)
    )
    text = preflight["committed_translation_text"]
    assert "\r\n" in text

    for mutated in (text.replace("\r\n", "\n"), text + " "):
        assert mutated != text
        assert _sha256_text(mutated) != preflight[
            "committed_translation_sha256"
        ]
        with pytest.raises(
            g7o.G7ORecoveryError, match="no longer matches its bound identity"
        ):
            g7o.validate_fresh_recovery_authorization(
                _authorization(preflight),
                preflight={**preflight, "committed_translation_text": mutated},
            )


def test_crlf_committed_sha_field_tamper_fails_closed(crlf_env):
    preflight = g7o.preflight_ch1_state_recovery(
        **_preflight_kwargs(crlf_env)
    )
    normalized_sha256 = hashlib.sha256(
        crlf_env.committed.read_bytes().replace(b"\r\n", b"\n")
    ).hexdigest()

    for tampered in ("0" * 64, normalized_sha256):
        assert tampered != preflight["committed_translation_sha256"]
        envelope = {**preflight, "committed_translation_sha256": tampered}
        with pytest.raises(g7o.G7ORecoveryError):
            g7o._validated_preflight(envelope)
        with pytest.raises(g7o.G7ORecoveryError):
            g7o.validate_fresh_recovery_authorization(
                _authorization(preflight), preflight=envelope
            )


def test_crlf_recovery_request_binds_exact_committed_bytes(
    crlf_env, tmp_path, monkeypatch
):
    preflight = g7o.preflight_ch1_state_recovery(
        **_preflight_kwargs(crlf_env)
    )
    messages = g7o._state_extraction_messages(preflight)
    content = messages[1]["content"]
    raw = content.encode("utf-8")
    text = preflight["committed_translation_text"]

    assert content.endswith(text)
    assert raw.endswith(COMMITTED_TRANSLATION_CRLF_BYTES)
    assert COMMITTED_TRANSLATION_CRLF_BYTES in raw
    assert "\r\n" in content
    assert _sha256_text(text) == preflight["committed_translation_sha256"]
    assert raw.count(COMMITTED_TRANSLATION_CRLF_BYTES) == 1

    # Deterministic across two independent CRLF builds.
    other = _prepare_crlf(tmp_path / "second-build", monkeypatch=monkeypatch)
    other_preflight = g7o.preflight_ch1_state_recovery(
        **_preflight_kwargs(other)
    )
    assert other_preflight["committed_translation_sha256"] == (
        preflight["committed_translation_sha256"]
    )
    assert other_preflight["committed_translation_text"] == text
    assert g7o._state_extraction_messages(other_preflight) == messages


def test_crlf_structural_paths_absent_and_zero_provider_zero_credential(
    crlf_env, monkeypatch
):
    calls, factory, reader = _guards(monkeypatch)
    monkeypatch.setattr(os, "getenv", factory)
    before = _snapshot(crlf_env.evidence_root)

    assert g7o.assert_no_translation_or_chapter2_path() is None
    source = Path(g7o.__file__).read_text(encoding="utf-8")
    for symbol in g7o._FORBIDDEN_PATH_SYMBOLS:
        assert symbol not in source

    preflight = g7o.preflight_ch1_state_recovery(
        **_preflight_kwargs(crlf_env)
    )
    authorization = g7o.validate_fresh_recovery_authorization(
        _authorization(preflight), preflight=preflight
    )
    assert authorization["provider_calls"] == 0
    assert preflight["provider_calls"] == 0
    assert preflight["provider_client_constructed"] is False
    assert preflight["credential_access"] is False
    assert preflight["chapter_1_retranslated"] is False
    assert preflight["chapter_1_translation_retry"] is False
    assert preflight["chapter_2_execution"] is False
    assert preflight["progress"] == {
        "completed": [1],
        "last_completed": 1,
        "next_chapter": 2,
    }

    assert calls == {"client_factory": 0, "secret_reader": 0}
    assert _snapshot(crlf_env.evidence_root) == before


def test_crlf_empty_response_fails_closed_without_json_repair(crlf_env):
    built = crlf_env
    state = g7o.preflight_ch1_state_recovery(**_preflight_kwargs(built))
    before = _state_bytes(built)
    assert len(before) == 3
    names_before = {path.name for path in _journal_dir(built).iterdir()}
    assert names_before == {
        "0001.g7c_state_extraction.json",
        "0001.g7c_state_json_repair.json",
        "0001.g7l_ch1_state_recovery_repair.json",
    }

    outcome, client = _run(
        built,
        state,
        [
            _provider_response(
                "",
                finish_reason=g7j.FINISH_REASON_LENGTH,
                completion_tokens=8_000,
                reasoning_tokens=8_000,
            )
        ],
    )
    assert outcome["status"] == g7o.RECOVERY_FAIL_CLOSED_STATUS
    assert outcome["classification"] == (
        g7j.AUXILIARY_BUDGET_EXHAUSTED_EMPTY_CONTENT
    )
    assert outcome["fail_closed"] is True
    assert outcome["recovered_state"] is None
    assert outcome["state_source"] is None
    assert outcome["repair_send_used"] is False
    assert outcome["apply_performed"] is False
    assert outcome["provider_calls"] == 1
    assert outcome["new_provider_sends_started"] == 1
    assert "no JSON repair" in outcome["blocker"]
    assert len(client.calls) == 1

    with pytest.raises(g7o.G7ORecoveryError):
        g7o._state_json_repair_messages("")

    names = {path.name for path in _journal_dir(built).iterdir()}
    assert names - names_before == {
        "0001.g7n_ch1_state_recovery_8k_repair.json"
    }
    assert [
        name for name in names if "state_json_repair" in name
    ] == ["0001.g7c_state_json_repair.json"]
    assert _state_bytes(built) == before


def test_crlf_historical_g7c_g7f_protections_unchanged(crlf_env):
    built = crlf_env
    preflight = g7o.preflight_ch1_state_recovery(
        **_preflight_kwargs(built)
    )
    progress_path = built.run_root / "data" / "progress.json"
    protected = {
        "extraction": built.extraction_journal.read_bytes(),
        "repair": built.repair_journal.read_bytes(),
        "ledger": built.historical_ledger.read_bytes(),
        "committed": built.committed.read_bytes(),
        "qa": built.qa.read_bytes(),
        "progress": progress_path.read_bytes(),
    }
    assert protected["committed"] == COMMITTED_TRANSLATION_CRLF_BYTES
    assert json.loads(protected["progress"].decode("utf-8"))[
        "next_chapter"
    ] == 2

    result, client = _execute_success(
        built, preflight, first_content="{malformed state json"
    )
    assert len(client.calls) == 2
    assert result["status"] == g7o.RECOVERY_APPLIED_STATUS
    assert result["plan_requires_rematerialization"] is False
    assert result["historical_evidence_modified"] is False
    assert result["chapter_2_execution"] is False
    assert result["chapter_1_retranslated"] is False

    # The historical g7c_* journals and the historical G7F ledger are untouched,
    # and no fixed g7c_* slot was rewritten.
    journal_dir = _journal_dir(built)
    assert built.extraction_journal.read_bytes() == protected["extraction"]
    assert built.repair_journal.read_bytes() == protected["repair"]
    assert built.historical_ledger.read_bytes() == protected["ledger"]
    g7c_prefix = f"0001.{g7n.G7C_JOURNAL_KIND_PREFIX}"
    assert sorted(
        path.name
        for path in journal_dir.iterdir()
        if path.name.startswith(g7c_prefix)
    ) == [
        "0001.g7c_state_extraction.json",
        "0001.g7c_state_json_repair.json",
    ]
    assert preflight["original_journals"]["state_extraction"][
        "file_sha256"
    ] == hashlib.sha256(protected["extraction"]).hexdigest()
    assert preflight["original_journals"]["state_json_repair"][
        "file_sha256"
    ] == hashlib.sha256(protected["repair"]).hexdigest()
    assert preflight["original_ledger"]["file_sha256"] == hashlib.sha256(
        protected["ledger"]
    ).hexdigest()

    # The recovery journal/ledger namespace stays separate.
    recovery_journal = g7o.recovery_journal_path(journal_dir)
    recovery_ledger = g7o.recovery_ledger_path(built.run_root)
    assert recovery_journal.name in {
        path.name for path in journal_dir.iterdir()
    }
    assert not recovery_journal.name.startswith(g7c_prefix)
    assert recovery_ledger != built.historical_ledger
    assert recovery_ledger.is_file()
    assert recovery_ledger.read_bytes() != protected["ledger"]

    # The committed Chapter-1 bytes, the QA bytes and the progress file are
    # byte-identical, with Chapter 2 still merely next.
    assert built.committed.read_bytes() == protected["committed"]
    assert hashlib.sha256(built.committed.read_bytes()).hexdigest() == (
        preflight["committed_translation_sha256"]
    )
    assert built.qa.read_bytes() == protected["qa"]
    assert hashlib.sha256(built.qa.read_bytes()).hexdigest() == (
        preflight["qa_sha256"]
    )
    assert progress_path.read_bytes() == protected["progress"]
    applied = result["applied_state"]
    assert applied["progress_unchanged"] is True
    assert applied["chapter_2_advanced"] is False
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    assert progress == json.loads(protected["progress"].decode("utf-8"))
    assert progress["completed"] == [1]
    assert progress["last_completed"] == 1
    assert progress["next_chapter"] == 2



# --------------------------------------------------------------------------
# 20. G7O 8K policy binding, terminal evidence and identity separation
# --------------------------------------------------------------------------


def test_preflight_binds_8k_policy_and_terminal_evidence(env, preflight):
    assert preflight["corrected_auxiliary_budget"] == 8_000
    assert preflight["legacy_corrected_auxiliary_budget"] == 4_000
    assert preflight["policy_revision"] == g7j.GOAT_AUXILIARY_8K_REVISION
    assert preflight["terminal_evidence_modified"] is False
    assert preflight["auxiliary_policy_binding"] == dict(
        g7o.G7O_AUXILIARY_POLICY_BINDING
    )
    terminal = preflight["terminal_evidence"]
    assert terminal["journal_status"] == "response_recorded"
    assert terminal["journal_kind"] == "g7l_ch1_state_recovery_repair"
    assert terminal["provider_send_count"] == 1
    assert terminal["completion_tokens"] == 4_000
    assert terminal["reasoning_tokens"] == 4_000
    assert terminal["visible_content"] == ""
    assert terminal["finish_reason"] == "length"
    assert terminal["classification"] == (
        g7j.AUXILIARY_BUDGET_EXHAUSTED_EMPTY_CONTENT
    )
    assert terminal["repair_performed"] is False
    assert terminal["apply_performed"] is False
    assert g7o.RECOVERY_MAX_COMPLETION_TOKENS == 8_000
    assert g7o.RECOVERY_MAX_COMPLETION_TOKENS != (
        g7j.GOAT_AUXILIARY_TOTAL_COMPLETION_TOKENS
    )
    assert g7j.goat_auxiliary_8k_completion_budget_for_identity(
        goat=True
    ) == 8_000
