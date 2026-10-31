"""Zero-Provider tests for the G7R dynamic Stage-1 Chapters 3-24 wave controller.

Every test here is local and synthetic: no test constructs a Provider client,
reads a credential, reads ``.env``, or executes a paid chapter.  Paid execution
is exercised only through injected fakes that never reach a transport.

The one deliberate seam is ``g7f._validate_run_binding``: the frozen G7F binding
gate validates a synthetic root against the frozen G7C roots-manifest bytes and
the frozen source/chapter-library SHAs, which cannot be reproduced from a
temporary fixture.  Tests replace only that read-only gate so the rest of the
G7F plan/executor path (plan envelope, G7E approval token, state fingerprint,
ledger, and journal) stays real.  The real gate is exercised live against the
real Stage-1 evidence root outside this suite.

Valid G7F ledger fixtures are NOT hand-written field subsets: they are created
with the real frozen ``InvocationBudgetLedger`` machinery (``open_or_create``
plus real ``begin_send`` records and the frozen G7D ``record_accepted_usage``
pricing path) bound to the exact materialized plan, so every accepted-accounting
fixture is a genuinely canonical, plan-bound G7F ledger.
"""

from __future__ import annotations

import builtins
import copy
import hashlib
import importlib.util
import json
import socket
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

import benchmark_longitudinal_g7c_stage1_integration as g7c
import benchmark_longitudinal_g7f_stage1_paid_executor as g7f
import benchmark_longitudinal_g7g_stage1_secure_launcher as g7g
import benchmark_longitudinal_g7r_stage1_ch3_24_wave_controller as g7r
import benchmark_longitudinal_g7s_stage1_durable_quarantine as g7s
from benchmark_longitudinal_config import PROVIDER_NAME


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = (
    REPO_ROOT / "benchmark_longitudinal_g7r_stage1_ch3_24_wave_controller.py"
)

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
SECRET_KEY = "sk-test-command-code-secret-0123456789abcdef"
PROTECTED_PREFIX = b"g7r-protected:"

SPECS = {
    index: spec
    for index, spec in enumerate(g7c.stage1_run_specs(), start=1)
}

STABLE_SHA = "a" * 64
REQUEST_SHA = "b" * 64
OPERATION_SHA = "c" * 64


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _canonical(value) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _canon_sha(value) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _journal_record(*, status, operation_kind, chapter, model, novel_id, response=None,
                    response_sha256=None, error=None):
    record = {
        "schema_version": 1,
        "revision": "paid-request-journal-v1",
        "operation_kind": operation_kind,
        "novel_id": novel_id,
        "chapter": chapter,
        "provider": PROVIDER_NAME,
        "model": model,
        "profile_id": "fixture",
        "source_sha256": STABLE_SHA,
        "translation_sha256": "",
        "request_sha256": REQUEST_SHA,
        "operation_id": OPERATION_SHA,
        "status": status,
        "attempt_context": {},
        "response": response if response is not None else {},
        "response_sha256": response_sha256 if response_sha256 is not None else "",
        "error": error or {},
        "created_utc": NOW.isoformat(),
        "updated_utc": NOW.isoformat(),
    }
    record["record_sha256"] = _canon_sha(
        {key: value for key, value in record.items() if key != "record_sha256"}
    )
    return record


def _response_recorded_response(content, finish_reason, completion_tokens=100,
                                diagnostic=None, stats=None):
    response = {
        "content": content,
        "finish_reason": finish_reason,
        "stats": (
            stats
            if stats is not None
            else {
                "prompt_tokens": 10,
                "completion_tokens": completion_tokens,
                "cache_hit_tokens": 0,
                "cache_miss_tokens": 10,
            }
        ),
    }
    if diagnostic is not None:
        response["diagnostic"] = diagnostic
    return response


def _write_journal(run_root: Path, name: str, record) -> None:
    journal_dir = run_root / "data" / "provider_request_journal"
    journal_dir.mkdir(parents=True, exist_ok=True)
    (journal_dir / name).write_text(
        json.dumps(record, indent=2, sort_keys=True), encoding="utf-8"
    )


def _healthy_pair(run_root: Path, spec) -> None:
    """Seed one healthy chapter-1/2 journal set (non-empty, stop-terminated)."""
    model = spec["model_id"]
    novel = spec["novel_id"]
    for chapter in (1, 2):
        for operation_kind, content in (
            ("translation", "translated prose"),
            ("state_extraction", '{"new_terms": [], "issues": []}'),
        ):
            response = _response_recorded_response(
                content,
                "stop",
                diagnostic=(
                    {
                        "status": "ok",
                        "visible_content_empty": False,
                        "finish_reason": "stop",
                        "completion_tokens": 100,
                        "max_completion_tokens": 24000,
                        "near_completion_ceiling": False,
                    }
                    if operation_kind == "translation"
                    else None
                ),
            )
            _write_journal(
                run_root,
                f"{chapter:04d}.g7c_{operation_kind}.json",
                _journal_record(
                    status="response_recorded",
                    operation_kind=operation_kind,
                    chapter=chapter,
                    model=model,
                    novel_id=novel,
                    response=response,
                    response_sha256=_canon_sha(response),
                ),
            )


def _uncertain_journal(run_root: Path, name: str, *, operation_kind, chapter, spec):
    _write_journal(
        run_root,
        name,
        _journal_record(
            status="uncertain",
            operation_kind=operation_kind,
            chapter=chapter,
            model=spec["model_id"],
            novel_id=spec["novel_id"],
            error={"type": "InternalServerError", "message": "524 upstream"},
        ),
    )


def _terminal_auxiliary_journal(
    run_root: Path, name: str, *, operation_kind, chapter, spec, completion_tokens=8000
):
    response = _response_recorded_response(
        "", "length", completion_tokens=completion_tokens
    )
    _write_journal(
        run_root,
        name,
        _journal_record(
            status="response_recorded",
            operation_kind=operation_kind,
            chapter=chapter,
            model=spec["model_id"],
            novel_id=spec["novel_id"],
            response=response,
            response_sha256=_canon_sha(response),
        ),
    )


def _terminal_translation_journal(
    run_root, name, *, chapter, spec, content="rejected by policy",
    finish_reason="content-filter",
):
    response = _response_recorded_response(
        content,
        finish_reason,
        completion_tokens=0,
        diagnostic={
            "status": "unsupported_translation_finish_reason",
            "visible_content_empty": False,
            "finish_reason": finish_reason,
            "completion_tokens": 0,
            "max_completion_tokens": 24000,
            "near_completion_ceiling": False,
        },
    )
    _write_journal(
        run_root,
        name,
        _journal_record(
            status="response_recorded",
            operation_kind="translation",
            chapter=chapter,
            model=spec["model_id"],
            novel_id=spec["novel_id"],
            response=response,
            response_sha256=_canon_sha(response),
        ),
    )


def _write_run(
    root: Path,
    index: int,
    *,
    progress_last: int,
    journals=(),
    novel_extra=None,
) -> Path:
    spec = SPECS[index]
    run_root = root / spec["relative_root"]
    (run_root / "data" / "provider_request_journal").mkdir(parents=True, exist_ok=True)
    (run_root / "data" / "g7f_invocation_ledgers").mkdir(
        parents=True, exist_ok=True
    )
    (run_root / "output" / "chapters").mkdir(parents=True, exist_ok=True)
    (run_root / "output" / "pending").mkdir(parents=True, exist_ok=True)

    identity = {
        "schema_version": 1,
        "g7c_revision": "goat-g7c-stage1-execution-integration-v1",
        "g7c_integration_sha256": g7c.G7C_INTEGRATION_SHA256,
        **{
            key: copy.deepcopy(spec[key])
            for key in (
                "run_index",
                "model_id",
                "novel_id",
                "run_sha256",
                "run_plan_sha256",
                "s0_identity_sha256",
                "source_sha256",
                "chapter_library_sha256",
                "profile",
                "audit_interval",
                "chapter_numbering",
                "stage1_chapters",
                "stage1_audit_checkpoints",
                "relative_root",
            )
        },
        "source_config_sha256": STABLE_SHA,
        "source_addendum_sha256": STABLE_SHA,
        "provider_calls": 0,
        "state": "s0_unexecuted",
    }
    identity["identity_sha256"] = _canon_sha(identity)
    (run_root / "g7c_run_identity.json").write_text(
        json.dumps(identity, indent=2, sort_keys=True), encoding="utf-8"
    )

    novel = {
        "id": spec["novel_id"],
        "title": f"Fixture {spec['novel_id']}",
        "source_language": "zh",
        "target_language": "en",
        "profile": spec["profile"],
        "final_chapter": 30,
        "audit_interval": spec["audit_interval"],
        "chapter_numbering": spec["chapter_numbering"],
        # These recorded runs predate repair being on by default.
        "residual_cjk_micro_repair": {"enabled": False},
        "glossary_candidate_auto_resolution": {"enabled": False},
    }
    if spec["novel_id"] == "rogue_first_prince":
        # Mirrors the real Stage-1 rogue novel.json opt-in exactly.
        novel["source_type"] = "txt"
        novel["residual_cjk_micro_repair"] = {
            "enabled": True,
            "max_total_cjk_chars": 12,
            "max_fragments": 6,
            "max_affected_lines": 3,
            "max_attempts_per_pending_version": 1,
        }
    novel.update(novel_extra or {})
    (run_root / "novel.json").write_text(
        json.dumps(novel, indent=2, sort_keys=True), encoding="utf-8"
    )

    chapters = [
        {
            "chapter": number,
            "character_count": 3000 + number * 10,
            "text": "source text",
        }
        for number in range(1, 31)
    ]
    (run_root / "data" / "chapters.json").write_text(
        json.dumps(chapters, indent=2, sort_keys=True), encoding="utf-8"
    )

    progress = {
        "completed": list(range(1, progress_last + 1)),
        "last_completed": progress_last,
        "next_chapter": progress_last + 1,
        "failed": [],
        "last_audited_chapter": 0,
    }
    (run_root / "data" / "progress.json").write_text(
        json.dumps(progress, indent=2, sort_keys=True), encoding="utf-8"
    )

    for name, record in journals:
        _write_journal(run_root, name, record)

    return run_root


def _fixture_grid_root(tmp_path: Path) -> Path:
    """Mirror the current twelve-cell Stage-1 cohort evidence."""
    root = tmp_path / "stage1"
    for index in range(1, 13):
        spec = SPECS[index]
        run_root = _write_run(root, index, progress_last=0)
        if index in (1, 2, 11, 12):
            continue
        if index == 6:
            _write_run(root, 6, progress_last=1)
            run_root = root / spec["relative_root"]
            _healthy_pair(run_root, spec)
            # chapter-1 journals only, then an uncertain chapter-2 translation
            for path in (run_root / "data" / "provider_request_journal").glob(
                "0002.*"
            ):
                path.unlink()
            _uncertain_journal(
                run_root,
                "0002.g7c_translation.json",
                operation_kind="translation",
                chapter=2,
                spec=spec,
            )
            continue
        run_root = _write_run(root, index, progress_last=2)
        _healthy_pair(run_root, spec)

    # Run 01: chapter 1 committed, chapter-1 state extraction terminally empty.
    run_root = _write_run(root, 1, progress_last=1)
    spec = SPECS[1]
    (run_root / "output" / "chapters" / "0001.md").write_text(
        "chapter one", encoding="utf-8"
    )
    _healthy_pair(run_root, spec)
    _terminal_auxiliary_journal(
        run_root,
        "0001.g7c_state_extraction.json",
        operation_kind="state_extraction",
        chapter=1,
        spec=spec,
        completion_tokens=3000,
    )

    # Run 02: chapter-2 state extraction terminally empty at the 8K ceiling.
    run_root = _write_run(root, 2, progress_last=1)
    spec = SPECS[2]
    _healthy_pair(run_root, spec)
    _terminal_auxiliary_journal(
        run_root,
        "0002.g7c_state_extraction.json",
        operation_kind="state_extraction",
        chapter=2,
        spec=spec,
    )

    # Run 11: chapter-1 primary translation durably rejected (content filter).
    run_root = _write_run(root, 11, progress_last=0)
    spec = SPECS[11]
    _terminal_translation_journal(
        run_root, "0001.g7c_translation.json", chapter=1, spec=spec
    )

    # Run 12: chapter-1 state extraction uncertain (ambiguous transport).
    run_root = _write_run(root, 12, progress_last=0)
    spec = SPECS[12]
    _uncertain_journal(
        run_root,
        "0001.g7c_state_extraction.json",
        operation_kind="state_extraction",
        chapter=1,
        spec=spec,
    )

    return root


def _all_ready_root(base: Path, *, progress_last: int = 2) -> Path:
    root = base / "stage1"
    for index in range(1, 13):
        run_root = _write_run(root, index, progress_last=progress_last)
        _healthy_pair(run_root, SPECS[index])
    return root


def _patch_run_binding(monkeypatch) -> None:
    def fake_validate_run_binding(stage1_root, run_index):
        root = Path(stage1_root).resolve()
        spec = copy.deepcopy(SPECS[int(run_index)])
        run_root = (root / spec["relative_root"]).resolve()
        return {
            "stage1_root": root,
            "run_root": run_root,
            "spec": spec,
            "manifest": {"run_count": 12},
            "run_identity": {"identity_sha256": "0" * 64},
        }

    monkeypatch.setattr(g7f, "_validate_run_binding", fake_validate_run_binding)


def _tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(
        (item for item in root.rglob("*") if item.is_file()),
        key=lambda item: item.relative_to(root).as_posix(),
    ):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


class FakeDpapi:
    def __init__(self, *, fail_unprotect: bool = False):
        self.fail_unprotect = fail_unprotect
        self.unprotect_calls: list[bytes] = []
        self.events: list[str] = []

    def protect(self, plaintext: bytes) -> bytes:
        return PROTECTED_PREFIX + bytes(plaintext)[::-1]

    def unprotect(self, ciphertext: bytes) -> bytes:
        self.events.append("unprotect")
        self.unprotect_calls.append(bytes(ciphertext))
        if self.fail_unprotect:
            raise ValueError("corrupted protected secret")
        if not bytes(ciphertext).startswith(PROTECTED_PREFIX):
            raise ValueError("not a protected envelope")
        return bytes(ciphertext)[len(PROTECTED_PREFIX):][::-1]


def _write_secret(base: Path, dpapi: FakeDpapi) -> Path:
    path = base / "command_code_api_key.dpapi"
    path.write_bytes(dpapi.protect(SECRET_KEY.encode("utf-8")))
    return path


LEDGER_REQUEST_BODY = {
    "model": "fixture",
    "messages": [{"role": "user", "content": "x"}],
    "max_completion_tokens": 100,
}


def _canonical_ledger(
    run_root: Path,
    plan,
    *,
    sends: int = 1,
    accepted_usage: tuple[int, int] | None = None,
):
    """Create one REAL, canonical, exact-plan-bound G7F ledger.

    This uses only the frozen ``InvocationBudgetLedger`` machinery: the
    ``open_or_create`` document writer and real ``begin_send`` records with
    contiguous send indexes and valid request identities.  When
    ``accepted_usage`` is supplied, the real frozen G7D accounting path prices
    the accepted completions through ``record_accepted_usage``, so the accepted
    credit-equivalent is authoritative rather than fabricated.  The result
    passes ``InvocationBudgetLedger.open_or_create(..., allow_create=False)``
    including exact ``_assert_plan_binding``.
    """
    ledger = g7f.InvocationBudgetLedger.open_or_create(
        run_root=run_root, plan=plan, clock=lambda: NOW
    )
    for index in range(1, sends + 1):
        ledger.begin_send(
            logical_operation="translation",
            chapter=plan["target_chapter"],
            request_sha256=REQUEST_SHA if index == 1 else f"{index:064d}",
            request_body=LEDGER_REQUEST_BODY,
            request_started_utc=NOW,
        )
    if accepted_usage is not None:
        prompt_tokens, completion_tokens = accepted_usage
        ledger.record_accepted_usage(
            send_index=1,
            request_sha256=REQUEST_SHA,
            usage=SimpleNamespace(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cached_tokens_exposed=False,
                cached_tokens=None,
            ),
            request_started_utc=NOW,
        )
    return ledger


def _commit_chapter(run_root: Path, target: int) -> None:
    """Persist the chapter-commit artifacts without touching the G7F ledger."""
    (run_root / "output" / "chapters" / f"{target:04d}.md").write_text(
        f"chapter {target}", encoding="utf-8"
    )
    progress_path = run_root / "data" / "progress.json"
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    progress["completed"] = sorted(set(progress["completed"]) | {target})
    progress["last_completed"] = target
    progress["next_chapter"] = target + 1
    progress_path.write_text(
        json.dumps(progress, indent=2, sort_keys=True), encoding="utf-8"
    )


def _simulate_success(
    run_root: Path,
    target: int,
    plan,
    *,
    accepted_usage: tuple[int, int] | None = None,
) -> None:
    _commit_chapter(run_root, target)
    _canonical_ledger(
        run_root, plan, sends=1, accepted_usage=accepted_usage
    )


def _write_corrupt_ledger(run_root: Path, target: int) -> Path:
    """Write one unreadable target-chapter G7F ledger for fail-close tests."""
    ledger_path = g7f.invocation_ledger_path(run_root, target)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text("{not a json ledger", encoding="utf-8")
    return ledger_path


class FakePreflight:
    def __init__(self, *, failing: tuple[int, ...] = (), events=None):
        self.failing = set(failing)
        self.events = events if events is not None else []
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        plan = json.loads(Path(kwargs["plan_path"]).read_text(encoding="utf-8"))
        run_index = plan["run_index"]
        self.events.append(f"preflight:{run_index}")
        if run_index in self.failing:
            raise g7g.G7GSecureLauncherError("synthetic preflight refusal")
        return {
            "status": "PREFLIGHT_OK",
            "plan_sha256": plan["plan_sha256"],
            "approval_token": plan["approval_token"],
            "run_index": run_index,
            "target_chapter": plan["target_chapter"],
            "provider_calls": 0,
        }


class FakeExecutor:
    """Injected stand-in for ``g7f.execute_stage1_invocation``."""

    def __init__(self, *, handler, events=None, printer=None):
        self.handler = handler
        self.events = events if events is not None else []
        self.printer = printer
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        plan = kwargs["plan"]
        run_index = plan["run_index"]
        self.events.append(f"execute:{run_index}")
        assert kwargs["operator_authorized"] is True
        assert kwargs["api_key"] == SECRET_KEY
        if self.printer is not None:
            self.printer(run_index)
        return self.handler(self, kwargs)


def _bundle_for(
    tmp_path: Path,
    monkeypatch,
    *,
    chapter: int,
    root: Path,
    head: str = "f" * 40,
) -> tuple[Path, dict]:
    _patch_run_binding(monkeypatch)
    bundle = g7r.plan_wave(
        stage1_root=root,
        target_chapter=chapter,
        control_dir=tmp_path / "control",
        repository_head=head,
        repository_head_resolver=lambda _root: head,
    )
    return g7r.wave_bundle_path(tmp_path / "control", chapter), bundle


def _execute(
    tmp_path,
    *,
    bundle_path,
    bundle,
    dpapi,
    preflight,
    executor,
    secret_path=None,
    **overrides,
):
    kwargs = {
        "stage1_root": bundle["stage1_root"],
        "bundle_path": bundle_path,
        "expected_bundle_sha256": bundle["bundle_sha256"],
        "authorize_paid_wave": True,
        "secret_path": secret_path
        if secret_path is not None
        else _write_secret(tmp_path, dpapi),
        "dpapi": dpapi,
        "preflight_fn": preflight,
        "execute_fn": executor,
        "repository_head_resolver": lambda _root: bundle["repository_head"],
        "clock": lambda: NOW,
    }
    kwargs.update(overrides)
    return g7r.execute_wave(**kwargs)


# ---------------------------------------------------------------------------
# A. import safety
# ---------------------------------------------------------------------------


def test_import_is_zero_write_zero_network_zero_credential(monkeypatch, tmp_path):
    before = sorted(
        str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*")
    )

    def forbid(*_args, **_kwargs):
        raise AssertionError("module import must not perform this operation")

    monkeypatch.setattr(socket, "socket", forbid)
    monkeypatch.setattr(socket, "create_connection", forbid)
    monkeypatch.setattr(socket, "getaddrinfo", forbid)
    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name.split(".")[0] in ("openai", "dotenv"):
            raise AssertionError("import must not load the provider SDK")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.chdir(tmp_path)

    unique = f"_g7r_import_safety_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(unique, MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[unique] = module
    try:
        spec.loader.exec_module(module)
        after = sorted(
            str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*")
        )
        assert after == before == []
        assert callable(module.main)
        assert callable(module.build_status_report)
        assert module.G7R_REVISION == (
            "goat-g7r-stage1-dynamic-wave-controller-v3"
        )
    finally:
        sys.modules.pop(unique, None)


def test_module_has_executable_main_guard():
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert 'if __name__ == "__main__":' in source
    assert "raise SystemExit(main())" in source


# ---------------------------------------------------------------------------
# B. CLI chapter bounds
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("chapter", [3, 4, 12, 24])
def test_target_chapter_bounds_accept_supported_chapters(chapter):
    assert g7r.assert_valid_target_chapter(chapter) == chapter


@pytest.mark.parametrize("chapter", [0, 1, 2, 25, 26, 30, 1214])
def test_target_chapter_bounds_reject_unsupported_chapters(chapter):
    with pytest.raises(g7r.G7RChapterBoundsError):
        g7r.assert_valid_target_chapter(chapter)


def test_chapter_25_is_rejected_because_periodic_audit_becomes_reachable():
    assert g7r._first_audit_chapter() == 25
    assert g7r.periodic_audit_due(24, last_audited_chapter=0, audit_interval=25) is False
    assert g7r.periodic_audit_due(25, last_audited_chapter=0, audit_interval=25) is True
    with pytest.raises(g7r.G7RChapterBoundsError, match="periodic audit"):
        g7r.assert_valid_target_chapter(25)


@pytest.mark.parametrize("command", ["status", "plan"])
def test_cli_rejects_out_of_range_chapter(tmp_path, capsys, command):
    args = [
        command,
        "--stage1-root",
        str(tmp_path),
        "--chapter",
        "25",
    ]
    if command == "plan":
        args += ["--control-dir", str(tmp_path / "control")]
    assert g7r.main(args) == 1
    document = json.loads(capsys.readouterr().out)
    assert document["status"] == "ERROR"
    assert document["provider_calls"] == 0


def test_cli_accepts_chapter_3_and_24_boundaries(tmp_path):
    for chapter, last in ((3, 2), (24, 23)):
        root = _all_ready_root(tmp_path / f"chapter_{chapter}", progress_last=last)
        report = g7r.build_status_report(
            stage1_root=root, target_chapter=chapter
        )
        assert report["target_chapter"] == chapter
        assert len(report["active_run_indices"]) == 12


# ---------------------------------------------------------------------------
# C. current-grid derivation
# ---------------------------------------------------------------------------


def test_current_grid_fixture_derives_expected_active_set(tmp_path):
    root = _fixture_grid_root(tmp_path)

    report = g7r.build_status_report(stage1_root=root, target_chapter=3)

    assert report["active_run_indices"] == [3, 4, 5, 7, 8, 9, 10]
    assert report["terminal_run_indices"] == [1, 2, 11]
    assert report["quarantined_run_indices"] == [6, 12]
    assert report["corrupt_run_indices"] == []
    assert report["disposition_counts"] == {
        "quarantined_ambiguous_transport": 2,
        "ready_for_target": 7,
        "terminal_durable_model_or_policy_failure": 3,
    }


def test_terminal_and_quarantined_dispositions_stay_distinct(tmp_path):
    root = _fixture_grid_root(tmp_path)

    report = g7r.build_status_report(stage1_root=root, target_chapter=3)
    by_index = {run["run_index"]: run for run in report["runs"]}

    assert by_index[1]["disposition"] == g7r.DISPOSITION_TERMINAL
    assert by_index[1]["terminal_evidence"][0]["failure_kind"] == (
        "auxiliary_empty_visible_content"
    )
    assert by_index[11]["disposition"] == g7r.DISPOSITION_TERMINAL
    assert by_index[11]["terminal_evidence"][0]["status"] == (
        "unsupported_translation_finish_reason"
    )

    for index in (6, 12):
        assert by_index[index]["disposition"] == g7r.DISPOSITION_QUARANTINED
        assert by_index[index]["terminal_evidence"] == []
        assert by_index[index]["uncertain_journals"]


def test_uncertain_journal_is_never_read_as_a_model_failure(tmp_path):
    root = tmp_path / "stage1"
    _write_run(root, 6, progress_last=1)
    spec = SPECS[6]
    run_root = root / spec["relative_root"]
    _healthy_pair(run_root, spec)
    _uncertain_journal(
        run_root,
        "0002.g7c_translation.json",
        operation_kind="translation",
        chapter=2,
        spec=spec,
    )

    disposition = g7r.derive_run_disposition(root, spec, 2)

    assert disposition["disposition"] == g7r.DISPOSITION_QUARANTINED
    assert disposition["evidence"]["terminal_findings"] == []


# ---------------------------------------------------------------------------
# D. dynamic cohort
# ---------------------------------------------------------------------------


def test_active_set_is_derived_and_not_a_hardcoded_seven(tmp_path):
    root = _all_ready_root(tmp_path)

    report = g7r.build_status_report(stage1_root=root, target_chapter=3)

    assert report["active_run_indices"] == list(range(1, 13))
    assert report["active_count"] == 12


def test_active_set_can_be_a_single_run(tmp_path):
    root = tmp_path / "stage1"
    for index in range(1, 13):
        _write_run(root, index, progress_last=0)
    spec = SPECS[7]
    run_root = _write_run(root, 7, progress_last=2)
    _healthy_pair(run_root, spec)

    report = g7r.build_status_report(stage1_root=root, target_chapter=3)

    assert report["active_run_indices"] == [7]


def test_terminal_at_chapter_n_is_excluded_from_chapter_n_plus_one(tmp_path):
    root = tmp_path / "stage1"
    for index in (3, 4):
        run_root = _write_run(root, index, progress_last=3)
        _healthy_pair(run_root, SPECS[index])
        (run_root / "output" / "chapters" / "0003.md").write_text(
            "chapter three", encoding="utf-8"
        )
    # Run 3's chapter-3 state extraction is a durable terminal rejection.
    run_root_3 = root / SPECS[3]["relative_root"]
    _terminal_auxiliary_journal(
        run_root_3,
        "0003.g7c_state_extraction.json",
        operation_kind="state_extraction",
        chapter=3,
        spec=SPECS[3],
    )

    report = g7r.build_status_report(stage1_root=root, target_chapter=4)

    assert report["active_run_indices"] == [4]
    assert report["terminal_run_indices"] == [3]


def test_uncertain_line_is_quarantined_and_excluded(tmp_path):
    root = tmp_path / "stage1"
    for index in (3, 4):
        run_root = _write_run(root, index, progress_last=3)
        _healthy_pair(run_root, SPECS[index])
    _uncertain_journal(
        root / SPECS[3]["relative_root"],
        "0004.g7c_state_extraction.json",
        operation_kind="state_extraction",
        chapter=4,
        spec=SPECS[3],
    )

    report = g7r.build_status_report(stage1_root=root, target_chapter=4)

    assert report["quarantined_run_indices"] == [3]
    assert report["active_run_indices"] == [4]


# ---------------------------------------------------------------------------
# E. plan mode
# ---------------------------------------------------------------------------


def test_plan_mode_is_zero_provider_and_leaves_stage1_unchanged(tmp_path, monkeypatch):
    root = _all_ready_root(tmp_path)
    before = _tree_hash(root)

    bundle_path, bundle = _bundle_for(
        tmp_path, monkeypatch, chapter=3, root=root
    )

    assert _tree_hash(root) == before
    assert bundle["status"] == g7r.BUNDLE_STATUS_READY
    assert bundle["active_run_indices"] == list(range(1, 13))
    assert bundle["zero_provider_verification"] == {
        "provider_calls": 0,
        "credential_access": False,
        "provider_client_constructed": False,
        "transport_sends": 0,
        "engine_loaded": False,
        "bundle_scope": "plan_only",
    }
    assert bundle_path.is_file()
    assert not (root / "control").exists()


def test_materialized_plan_sha_and_token_round_trip(tmp_path, monkeypatch):
    root = _all_ready_root(tmp_path)
    _bundle_for(tmp_path, monkeypatch, chapter=3, root=root)
    control = tmp_path / "control"
    bundle_dir = control / "wave_chapter_03"

    stored = json.loads(
        (bundle_dir / "g7r_wave_chapter_03_bundle.json").read_text(
            encoding="utf-8"
        )
    )
    assert stored["runs"]
    for record in stored["runs"]:
        plan_path = bundle_dir / record["plan_relative_path"]
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        assert hashlib.sha256(plan_path.read_bytes()).hexdigest() == (
            record["plan_file_sha256"]
        )
        assert plan["plan_sha256"] == record["plan_semantic_sha256"]
        inspection = g7f.inspect_stage1_invocation_plan(
            stage1_root=root, plan=plan
        )
        assert inspection["plan"]["approval_token"] == record["approval_token"]
        assert inspection["plan"]["plan_sha256"] == record["plan_semantic_sha256"]
        assert record["maximum_new_provider_sends"] >= 1
        assert Decimal(record["maximum_credit_equivalent_usd"]) > 0


def test_bundle_sha_round_trip_and_determinism(tmp_path, monkeypatch):
    root = _all_ready_root(tmp_path)
    _patch_run_binding(monkeypatch)

    first = g7r.plan_wave(
        stage1_root=root,
        target_chapter=3,
        control_dir=tmp_path / "control_a",
        repository_head="f" * 40,
        repository_head_resolver=lambda _root: "f" * 40,
    )
    second = g7r.plan_wave(
        stage1_root=root,
        target_chapter=3,
        control_dir=tmp_path / "control_b",
        repository_head="f" * 40,
        repository_head_resolver=lambda _root: "f" * 40,
    )

    # The semantic bundle SHA excludes only the explicitly documented volatile
    # fields, so the same root/chapter/head yields the same SHA in any control
    # directory: the bundle is location-independent.
    assert first["bundle_sha256"] == second["bundle_sha256"]
    assert first["created_utc"] != "" and second["created_utc"] != ""
    assert g7r.verify_wave_bundle(
        first, expected_bundle_sha256=first["bundle_sha256"]
    )["recomputed_bundle_sha256"] == first["bundle_sha256"]


def test_tampered_bundle_fails_closed(tmp_path, monkeypatch):
    root = _all_ready_root(tmp_path)
    bundle_path, bundle = _bundle_for(
        tmp_path, monkeypatch, chapter=3, root=root
    )

    tampered = copy.deepcopy(bundle)
    tampered["target_chapter"] = 4
    tampered_path = tmp_path / "tampered.json"
    tampered_path.write_text(json.dumps(tampered), encoding="utf-8")

    dpapi = FakeDpapi()
    with pytest.raises(g7r.G7RBundleError, match="does not recompute"):
        _execute(
            tmp_path,
            bundle_path=tampered_path,
            bundle=bundle,
            dpapi=dpapi,
            preflight=FakePreflight(),
            executor=FakeExecutor(handler=lambda self, kwargs: {}),
        )
    assert dpapi.unprotect_calls == []


def test_stale_policy_binding_fails_closed(tmp_path, monkeypatch):
    root = _all_ready_root(tmp_path)
    bundle_path, bundle = _bundle_for(
        tmp_path, monkeypatch, chapter=3, root=root
    )
    stale = copy.deepcopy(bundle)
    stale["policies"]["g7f_executor_sha256"] = "0" * 64
    stale["bundle_sha256"] = g7r.compute_bundle_sha256(stale)
    stale_path = tmp_path / "stale.json"
    stale_path.write_text(json.dumps(stale), encoding="utf-8")

    dpapi = FakeDpapi()
    with pytest.raises(g7r.G7RBundleError, match="stale repository policy"):
        _execute(
            tmp_path,
            bundle_path=stale_path,
            bundle=stale,
            dpapi=dpapi,
            preflight=FakePreflight(),
            executor=FakeExecutor(handler=lambda self, kwargs: {}),
        )
    assert dpapi.unprotect_calls == []


def test_stale_repository_head_fails_closed(tmp_path, monkeypatch):
    root = _all_ready_root(tmp_path)
    bundle_path, bundle = _bundle_for(
        tmp_path, monkeypatch, chapter=3, root=root
    )
    dpapi = FakeDpapi()

    with pytest.raises(g7r.G7RBundleError, match="stale repository HEAD"):
        _execute(
            tmp_path,
            bundle_path=bundle_path,
            bundle=bundle,
            dpapi=dpapi,
            preflight=FakePreflight(),
            executor=FakeExecutor(handler=lambda self, kwargs: {}),
            repository_head_resolver=lambda _root: "0" * 40,
        )
    assert dpapi.unprotect_calls == []


def test_stale_state_fingerprint_fails_closed(tmp_path, monkeypatch):
    root = _all_ready_root(tmp_path)
    bundle_path, bundle = _bundle_for(
        tmp_path, monkeypatch, chapter=3, root=root
    )
    run_root = root / SPECS[3]["relative_root"]
    (run_root / "data" / "glossary.json").write_text("{}", encoding="utf-8")

    dpapi = FakeDpapi()
    with pytest.raises(g7r.G7RBundleError, match="state fingerprint changed"):
        _execute(
            tmp_path,
            bundle_path=bundle_path,
            bundle=bundle,
            dpapi=dpapi,
            preflight=FakePreflight(),
            executor=FakeExecutor(handler=lambda self, kwargs: {}),
        )
    assert dpapi.unprotect_calls == []


# ---------------------------------------------------------------------------
# F. authorization
# ---------------------------------------------------------------------------


def test_execute_requires_authorize_flag_before_credential_access(tmp_path, monkeypatch):
    root = _all_ready_root(tmp_path)
    bundle_path, bundle = _bundle_for(
        tmp_path, monkeypatch, chapter=3, root=root
    )
    dpapi = FakeDpapi()

    with pytest.raises(g7r.G7RControllerError, match="authorize-paid-wave"):
        _execute(
            tmp_path,
            bundle_path=bundle_path,
            bundle=bundle,
            dpapi=dpapi,
            preflight=FakePreflight(),
            executor=FakeExecutor(handler=lambda self, kwargs: {}),
            authorize_paid_wave=False,
        )
    assert dpapi.unprotect_calls == []


def test_wrong_bundle_sha_fails_before_credential_access(tmp_path, monkeypatch):
    root = _all_ready_root(tmp_path)
    bundle_path, bundle = _bundle_for(
        tmp_path, monkeypatch, chapter=3, root=root
    )
    dpapi = FakeDpapi()

    with pytest.raises(g7r.G7RBundleError, match="SHA mismatch"):
        _execute(
            tmp_path,
            bundle_path=bundle_path,
            bundle=bundle,
            dpapi=dpapi,
            preflight=FakePreflight(),
            executor=FakeExecutor(handler=lambda self, kwargs: {}),
            expected_bundle_sha256="9" * 64,
        )
    assert dpapi.unprotect_calls == []


def test_one_bad_preflight_means_zero_secret_access_and_zero_sends(tmp_path, monkeypatch):
    root = _all_ready_root(tmp_path)
    bundle_path, bundle = _bundle_for(
        tmp_path, monkeypatch, chapter=3, root=root
    )
    dpapi = FakeDpapi()
    events: list[str] = []
    preflight = FakePreflight(failing=(5,), events=events)
    executor = FakeExecutor(
        handler=lambda self, kwargs: pytest.fail("no run may execute"),
        events=events,
    )

    with pytest.raises(g7r.G7RPreflightError):
        _execute(
            tmp_path,
            bundle_path=bundle_path,
            bundle=bundle,
            dpapi=dpapi,
            preflight=preflight,
            executor=executor,
        )

    assert dpapi.unprotect_calls == []
    assert executor.calls == []
    assert sum(event.startswith("execute:") for event in events) == 0


# ---------------------------------------------------------------------------
# G. G7G/G7F composition
# ---------------------------------------------------------------------------


def test_all_preflights_complete_before_first_secret_access(tmp_path, monkeypatch):
    root = _all_ready_root(tmp_path)
    bundle_path, bundle = _bundle_for(
        tmp_path, monkeypatch, chapter=3, root=root
    )
    order: list[str] = []
    dpapi = FakeDpapi()
    dpapi.events = order

    class OrderingPreflight(FakePreflight):
        def __call__(self, **kwargs):
            plan = json.loads(
                Path(kwargs["plan_path"]).read_text(encoding="utf-8")
            )
            order.append(f"preflight:{plan['run_index']}")
            return super().__call__(**kwargs)

    def handler(self, kwargs):
        _simulate_success(
            Path(bundle["runs"][0]["run_root"]), 3, kwargs["plan"]
        )
        return {"status": "COMPLETED"}

    executor = FakeExecutor(handler=handler, events=order)

    result = _execute(
        tmp_path,
        bundle_path=bundle_path,
        bundle=bundle,
        dpapi=dpapi,
        preflight=OrderingPreflight(),
        executor=executor,
        progress_log_path=tmp_path / "progress.log",
    )

    assert order.index("unprotect") > max(
        index for index, item in enumerate(order) if item.startswith("preflight")
    )
    assert result["authorization"]["preflight_count"] == len(bundle["runs"])
    assert result["authorization"]["secret_access_after_all_preflights"] is True


def test_execution_uses_the_direct_g7f_and_g7g_apis():
    assert g7r.execute_stage1_invocation is g7f.execute_stage1_invocation
    assert g7r.build_stage1_invocation_plan is g7f.build_stage1_invocation_plan
    assert g7r.inspect_stage1_invocation_plan is (
        g7f.inspect_stage1_invocation_plan
    )
    assert g7r.run_preflight is g7g.run_preflight


def test_success_is_not_inferred_from_the_transport_result_alone(tmp_path, monkeypatch):
    """A 'COMPLETED' transport result that changed nothing must not count."""
    root = _all_ready_root(tmp_path)
    bundle_path, bundle = _bundle_for(
        tmp_path, monkeypatch, chapter=3, root=root
    )
    dpapi = FakeDpapi()
    executor = FakeExecutor(
        handler=lambda self, kwargs: {"status": "COMPLETED"}
    )

    result = _execute(
        tmp_path,
        bundle_path=bundle_path,
        bundle=bundle,
        dpapi=dpapi,
        preflight=FakePreflight(),
        executor=executor,
    )

    assert result["completed_set"] == []
    assert result["status"] == g7r.WAVE_STOPPED_LOCAL_FAILURE
    assert result["fail_closed"] is True
    # Only the first run was attempted; the wave stopped fail-closed.
    assert len(executor.calls) == 1
    assert result["not_started_after_stop_set"] == bundle["active_run_indices"][1:]


# ---------------------------------------------------------------------------
# H. success
# ---------------------------------------------------------------------------


def test_success_advances_target_once_and_never_executes_next(tmp_path, monkeypatch):
    root = tmp_path / "stage1"
    for index in (3, 4):
        run_root = _write_run(root, index, progress_last=2)
        _healthy_pair(run_root, SPECS[index])

    bundle_path, bundle = _bundle_for(
        tmp_path, monkeypatch, chapter=3, root=root
    )
    assert bundle["active_run_indices"] == [3, 4]

    def handler(self, kwargs):
        record = next(
            item
            for item in bundle["runs"]
            if item["run_index"] == kwargs["plan"]["run_index"]
        )
        _simulate_success(Path(record["run_root"]), 3, kwargs["plan"])
        return {"status": "COMPLETED"}

    dpapi = FakeDpapi()
    executor = FakeExecutor(handler=handler)
    result = _execute(
        tmp_path,
        bundle_path=bundle_path,
        bundle=bundle,
        dpapi=dpapi,
        preflight=FakePreflight(),
        executor=executor,
        progress_log_path=tmp_path / "progress.log",
    )

    assert result["status"] == g7r.WAVE_COMPLETED
    assert result["completed_set"] == [3, 4]
    assert result["automatic_retry_performed"] is False
    assert result["automatic_next_chapter_execution"] is False
    assert len(executor.calls) == 2
    assert result["actual_new_provider_sends"] == 2

    for index in (3, 4):
        run_root = root / SPECS[index]["relative_root"]
        assert (run_root / "output" / "chapters" / "0003.md").is_file()
        assert not (run_root / "output" / "chapters" / "0004.md").exists()
        assert not g7f.invocation_ledger_path(run_root, 4).exists()
        assert not list(
            (run_root / "data" / "provider_request_journal").glob("0004.*")
        )
        progress = json.loads(
            (run_root / "data" / "progress.json").read_text(encoding="utf-8")
        )
        assert progress["last_completed"] == 3
        assert progress["next_chapter"] == 4

    assert result["next_active_set"] == [3, 4]
    assert result["original_ceilings"]["aggregate_maximum_new_provider_sends"] == (
        bundle["ceilings"]["aggregate_maximum_new_provider_sends"]
    )


# ---------------------------------------------------------------------------
# I. definitive terminal
# ---------------------------------------------------------------------------


def test_definitive_terminal_continues_to_the_next_independent_run(tmp_path, monkeypatch):
    root = tmp_path / "stage1"
    for index in (3, 4):
        run_root = _write_run(root, index, progress_last=2)
        _healthy_pair(run_root, SPECS[index])

    bundle_path, bundle = _bundle_for(
        tmp_path, monkeypatch, chapter=3, root=root
    )
    run_root_of = {
        item["run_index"]: Path(item["run_root"]) for item in bundle["runs"]
    }

    def handler(self, kwargs):
        run_index = kwargs["plan"]["run_index"]
        if run_index == 3:
            # A real G7F terminal outcome always crossed the paid boundary, so
            # its target ledger exists and records the started send.
            _canonical_ledger(
                run_root_of[3], kwargs["plan"], sends=1,
                accepted_usage=(1000, 500),
            )
            _terminal_auxiliary_journal(
                run_root_of[3],
                "0003.g7c_state_extraction.json",
                operation_kind="state_extraction",
                chapter=3,
                spec=SPECS[3],
            )
            raise g7f.G7FExecutionError(
                "G7L auxiliary response rejected: "
                "auxiliary_budget_exhausted_empty_content"
            )
        _simulate_success(run_root_of[4], 3, kwargs["plan"])
        return {"status": "COMPLETED"}

    dpapi = FakeDpapi()
    executor = FakeExecutor(handler=handler)
    result = _execute(
        tmp_path,
        bundle_path=bundle_path,
        bundle=bundle,
        dpapi=dpapi,
        preflight=FakePreflight(),
        executor=executor,
    )

    assert result["completed_set"] == [4]
    assert result["newly_terminal_set"] == [3]
    assert result["newly_quarantined_set"] == []
    assert result["status"] == g7r.WAVE_COMPLETED_WITH_TERMINAL_RUNS
    assert result["fail_closed"] is False
    # Exactly one attempt per run: a terminal run is never retried.
    attempted = [call["plan"]["run_index"] for call in executor.calls]
    assert attempted == [3, 4]
    assert result["next_active_set"] == [4]


# ---------------------------------------------------------------------------
# J. ambiguous transport
# ---------------------------------------------------------------------------


def test_ambiguous_transport_stops_the_entire_wave_without_retry(tmp_path, monkeypatch):
    root = tmp_path / "stage1"
    for index in (3, 4):
        run_root = _write_run(root, index, progress_last=2)
        _healthy_pair(run_root, SPECS[index])

    bundle_path, bundle = _bundle_for(
        tmp_path, monkeypatch, chapter=3, root=root
    )
    run_root_of = {
        item["run_index"]: Path(item["run_root"]) for item in bundle["runs"]
    }
    untouched = _tree_hash(run_root_of[4])
    created: dict[int, "g7f.InvocationBudgetLedger"] = {}

    def handler(self, kwargs):
        run_index = kwargs["plan"]["run_index"]
        created[run_index] = _canonical_ledger(
            run_root_of[run_index],
            kwargs["plan"],
            sends=1,
            accepted_usage=(1000, 500),
        )
        _uncertain_journal(
            run_root_of[run_index],
            "0003.g7c_translation.json",
            operation_kind="translation",
            chapter=3,
            spec=SPECS[run_index],
        )
        raise g7f.G7FExecutionError("transport blew up ambiguously")

    dpapi = FakeDpapi()
    executor = FakeExecutor(handler=handler)
    result = _execute(
        tmp_path,
        bundle_path=bundle_path,
        bundle=bundle,
        dpapi=dpapi,
        preflight=FakePreflight(),
        executor=executor,
    )

    assert result["status"] == g7r.WAVE_STOPPED_AMBIGUOUS
    assert result["fail_closed"] is True
    assert result["newly_quarantined_set"] == [3]
    assert result["newly_terminal_set"] == []
    assert result["not_started_after_stop_set"] == [4]
    assert [call["plan"]["run_index"] for call in executor.calls] == [3]
    assert _tree_hash(run_root_of[4]) == untouched
    # Ambiguous transport activity is included in the wave accounting and is
    # never inferred from stdout.
    assert result["actual_new_provider_sends"] == 1
    assert Decimal(result["accepted_credit_equivalent_usd"]) == (
        created[3].accepted_cost
    )
    assert result["actual_new_provider_sends_by_disposition"] == {
        g7r.RUN_OUTCOME_QUARANTINED: 1
    }


def test_corrupt_durable_evidence_after_send_quarantines_and_stops_the_wave(
    tmp_path, monkeypatch
):
    root = tmp_path / "stage1"
    for index in (3, 4):
        run_root = _write_run(root, index, progress_last=2)
        _healthy_pair(run_root, SPECS[index])

    bundle_path, bundle = _bundle_for(
        tmp_path, monkeypatch, chapter=3, root=root
    )
    run_root_of = {
        item["run_index"]: Path(item["run_root"]) for item in bundle["runs"]
    }
    untouched = _tree_hash(run_root_of[4])
    created: dict[int, "g7f.InvocationBudgetLedger"] = {}

    def handler(self, kwargs):
        run_index = kwargs["plan"]["run_index"]
        created[run_index] = _canonical_ledger(
            run_root_of[run_index],
            kwargs["plan"],
            sends=1,
            accepted_usage=(1000, 500),
        )
        (
            run_root_of[run_index]
            / "data"
            / "provider_request_journal"
            / "0003.bad.json"
        ).write_text("{not json", encoding="utf-8")
        raise g7f.G7FExecutionError("transport raised after a started send")

    dpapi = FakeDpapi()
    executor = FakeExecutor(handler=handler)
    result = _execute(
        tmp_path,
        bundle_path=bundle_path,
        bundle=bundle,
        dpapi=dpapi,
        preflight=FakePreflight(),
        executor=executor,
    )

    assert result["status"] == g7r.WAVE_STOPPED_AMBIGUOUS
    assert result["fail_closed"] is True
    assert result["newly_quarantined_set"] == [3]
    assert result["not_started_after_stop_set"] == [4]
    assert result["per_run_results"][0]["outcome"] == (
        g7r.RUN_OUTCOME_CORRUPT_STATE
    )
    # A corrupt post-send line is never retried: exactly one attempt.
    assert [call["plan"]["run_index"] for call in executor.calls] == [3]
    assert _tree_hash(run_root_of[4]) == untouched
    # Its paid-boundary ledger is still accounted.
    assert result["actual_new_provider_sends"] == 1
    assert Decimal(result["accepted_credit_equivalent_usd"]) == (
        created[3].accepted_cost
    )


def test_pre_send_local_failure_stays_local_without_quarantine(tmp_path, monkeypatch):
    root = tmp_path / "stage1"
    for index in (3, 4):
        run_root = _write_run(root, index, progress_last=2)
        _healthy_pair(run_root, SPECS[index])

    bundle_path, bundle = _bundle_for(
        tmp_path, monkeypatch, chapter=3, root=root
    )
    run_root_of = {
        item["run_index"]: Path(item["run_root"]) for item in bundle["runs"]
    }
    untouched = _tree_hash(run_root_of[4])

    def handler(self, kwargs):
        raise g7f.G7FExecutionError("local failure before any send")

    dpapi = FakeDpapi()
    executor = FakeExecutor(handler=handler)
    result = _execute(
        tmp_path,
        bundle_path=bundle_path,
        bundle=bundle,
        dpapi=dpapi,
        preflight=FakePreflight(),
        executor=executor,
    )

    assert result["status"] == g7r.WAVE_STOPPED_LOCAL_FAILURE
    assert result["fail_closed"] is True
    assert result["newly_quarantined_set"] == []
    assert result["per_run_results"][0]["outcome"] == (
        g7r.RUN_OUTCOME_LOCAL_FAILURE
    )
    assert [call["plan"]["run_index"] for call in executor.calls] == [3]
    assert _tree_hash(run_root_of[4]) == untouched
    assert result["actual_new_provider_sends"] == 0
    assert Decimal(result["accepted_credit_equivalent_usd"]) == Decimal("0")


# ---------------------------------------------------------------------------
# K. protected historical evidence
# ---------------------------------------------------------------------------


def test_protected_runs_are_never_selected_and_stay_byte_identical(tmp_path, monkeypatch):
    root = _fixture_grid_root(tmp_path)
    protected = [1, 2, 6, 11, 12]
    before = {
        index: _tree_hash(root / SPECS[index]["relative_root"])
        for index in protected
    }

    bundle_path, bundle = _bundle_for(
        tmp_path, monkeypatch, chapter=3, root=root
    )

    assert bundle["active_run_indices"] == [3, 4, 5, 7, 8, 9, 10]
    assert not set(protected) & set(bundle["active_run_indices"])
    for index in protected:
        assert _tree_hash(root / SPECS[index]["relative_root"]) == before[index]

    dpapi = FakeDpapi()
    executed: list[int] = []

    def handler(self, kwargs):
        index = kwargs["plan"]["run_index"]
        executed.append(index)
        _simulate_success(
            Path(
                next(
                    item["run_root"]
                    for item in bundle["runs"]
                    if item["run_index"] == index
                )
            ),
            3,
            kwargs["plan"],
        )
        return {"status": "COMPLETED"}

    _execute(
        tmp_path,
        bundle_path=bundle_path,
        bundle=bundle,
        dpapi=dpapi,
        preflight=FakePreflight(),
        executor=FakeExecutor(handler=handler),
    )

    assert set(executed).isdisjoint(protected)
    for index in protected:
        assert _tree_hash(root / SPECS[index]["relative_root"]) == before[index]


# ---------------------------------------------------------------------------
# L. cost / send enforcement
# ---------------------------------------------------------------------------


def test_envelopes_are_finite_positive_and_consistent(tmp_path, monkeypatch):
    root = _all_ready_root(tmp_path)
    _bundle_for(tmp_path, monkeypatch, chapter=3, root=root)
    bundle = json.loads(
        (
            tmp_path
            / "control"
            / "wave_chapter_03"
            / "g7r_wave_chapter_03_bundle.json"
        ).read_text(encoding="utf-8")
    )

    aggregate_sends = bundle["ceilings"]["aggregate_maximum_new_provider_sends"]
    aggregate_cost = Decimal(
        bundle["ceilings"]["aggregate_maximum_credit_equivalent_usd"]
    )
    assert isinstance(aggregate_sends, int) and aggregate_sends > 0
    assert aggregate_cost > 0 and aggregate_cost.is_finite()

    per_run_sends = 0
    per_run_cost = Decimal(0)
    for record in bundle["runs"]:
        sends = record["maximum_new_provider_sends"]
        cost = Decimal(record["maximum_credit_equivalent_usd"])
        derivation = record["envelope_derivation"]
        assert sends == derivation["total_max_new_sends"] == sum(
            derivation["operation_max_new_sends"].values()
        )
        assert derivation["periodic_audit_due"] is False
        assert derivation["operation_max_new_sends"]["periodic_audit"] == 0
        assert derivation["operation_max_new_sends"]["translation"] == 1
        assert derivation["prompt_token_upper_bound_per_send"] > 0
        assert cost > 0 and cost.is_finite()
        per_run_sends += sends
        per_run_cost += cost

    assert aggregate_sends == per_run_sends
    assert aggregate_cost == per_run_cost


def test_residual_cjk_and_auto_resolution_are_derived_from_config(tmp_path, monkeypatch):
    root = _all_ready_root(tmp_path)
    _bundle_for(tmp_path, monkeypatch, chapter=3, root=root)
    bundle = json.loads(
        (
            tmp_path
            / "control"
            / "wave_chapter_03"
            / "g7r_wave_chapter_03_bundle.json"
        ).read_text(encoding="utf-8")
    )

    for record in bundle["runs"]:
        derivation = record["envelope_derivation"]
        if record["novel_id"] == "rogue_first_prince":
            assert derivation["residual_cjk_micro_repair_enabled"] is True
            assert derivation["operation_max_new_sends"]["terminology_repair"] == 4
        else:
            assert derivation["residual_cjk_micro_repair_enabled"] is False
            assert derivation["operation_max_new_sends"]["terminology_repair"] == 2
        assert derivation["glossary_candidate_auto_resolution_enabled"] is False
        assert derivation["operation_max_new_sends"]["glossary_review"] == 4


def test_send_ceiling_blocks_before_transport(tmp_path, monkeypatch):
    _patch_run_binding(monkeypatch)
    root = tmp_path / "stage1"
    run_root = _write_run(root, 3, progress_last=2)
    _healthy_pair(run_root, SPECS[3])
    envelope = g7r.derive_run_envelope(root, SPECS[3], 3)
    plan = g7f.build_stage1_invocation_plan(
        stage1_root=root,
        run_index=3,
        target_chapter=3,
        maximum_new_provider_sends=envelope["maximum_new_provider_sends"],
        maximum_credit_equivalent_usd=envelope[
            "maximum_credit_equivalent_usd"
        ],
    )
    # The envelope send ceiling bounds the plan exactly.
    assert envelope["maximum_new_provider_sends"] == (
        plan["g7e_plan"]["maximum_new_provider_sends"]
    )

    ledger = g7f.InvocationBudgetLedger.open_or_create(
        run_root=run_root, plan=plan, clock=lambda: NOW
    )

    transport_calls: list = []

    def transport(_body):
        transport_calls.append(_body)
        raise AssertionError("transport must never be reached")

    body = {
        "model": "fixture",
        "messages": [{"role": "user", "content": "x"}],
        "max_completion_tokens": 100,
    }
    for send_index in range(1, envelope["maximum_new_provider_sends"] + 1):
        record = ledger.begin_send(
            logical_operation="translation",
            chapter=3,
            request_sha256=f"{send_index:064d}",
            request_body=body,
            request_started_utc=NOW,
        )
        assert record["send_index"] == send_index

    with pytest.raises(g7f.G7FBudgetError, match="send ceiling"):
        ledger.begin_send(
            logical_operation="translation",
            chapter=3,
            request_sha256="9" * 64,
            request_body=body,
            request_started_utc=NOW,
        )

    reloaded = g7f.InvocationBudgetLedger.open_or_create(
        run_root=run_root, plan=plan, clock=lambda: NOW, allow_create=False
    )
    assert reloaded.new_provider_sends_started == (
        envelope["maximum_new_provider_sends"]
    )
    assert transport_calls == []


# ---------------------------------------------------------------------------
# M. output
# ---------------------------------------------------------------------------


def test_status_stdout_is_one_json_document(tmp_path, capsys):
    root = _all_ready_root(tmp_path)

    assert g7r.main(["status", "--stage1-root", str(root), "--chapter", "3"]) == 0

    captured = capsys.readouterr()
    document = json.loads(captured.out)
    assert document["status"] == "OK"
    assert document["target_chapter"] == 3
    assert captured.err == ""


def test_plan_stdout_is_one_json_document(tmp_path, monkeypatch, capsys):
    root = _all_ready_root(tmp_path)
    _patch_run_binding(monkeypatch)

    code = g7r.main(
        [
            "plan",
            "--stage1-root",
            str(root),
            "--chapter",
            "3",
            "--control-dir",
            str(tmp_path / "control"),
        ]
    )

    assert code == 0
    document = json.loads(capsys.readouterr().out)
    assert document["status"] == g7r.BUNDLE_STATUS_READY
    # With no expected head supplied the ACTUAL checkout HEAD is recorded.
    assert document["repository_head"] == g7r.resolve_repository_head(REPO_ROOT)


def test_engine_progress_is_captured_outside_the_controller_stdout(tmp_path, monkeypatch, capsys):
    root = tmp_path / "stage1"
    run_root = _write_run(root, 3, progress_last=2)
    _healthy_pair(run_root, SPECS[3])

    bundle_path, bundle = _bundle_for(
        tmp_path, monkeypatch, chapter=3, root=root
    )

    def handler(self, kwargs):
        print("DEEPSEEK NOVEL TRANSLATION - UNATTENDED PRODUCTION")
        _simulate_success(
            Path(bundle["runs"][0]["run_root"]), 3, kwargs["plan"]
        )
        return {"status": "COMPLETED"}

    dpapi = FakeDpapi()
    result = _execute(
        tmp_path,
        bundle_path=bundle_path,
        bundle=bundle,
        dpapi=dpapi,
        preflight=FakePreflight(),
        executor=FakeExecutor(handler=handler, printer=None),
        progress_log_path=tmp_path / "progress.log",
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "UNATTENDED PRODUCTION" in captured.err
    assert "UNATTENDED PRODUCTION" in (tmp_path / "progress.log").read_text(
        encoding="utf-8"
    )
    assert result["status"] == g7r.WAVE_COMPLETED


def test_bundle_binds_the_frozen_policy_identities(tmp_path, monkeypatch):
    root = _all_ready_root(tmp_path)
    _, bundle = _bundle_for(tmp_path, monkeypatch, chapter=3, root=root)

    policies = bundle["policies"]
    assert policies["g7f_executor_sha256"] == (
        "7000b15e953b70312247322df0748f403826aba31118176a87186cd76e380219"
    )
    assert policies["g7p_stage1_runtime_auxiliary_policy_sha256"] == (
        "1246c5a6ec736e5e3246c02536eb72cb6a088a99f08f0bf5bcb2795d91f1a4e9"
    )
    assert policies["g7n_auxiliary_8k_policy_sha256"] == (
        "9c6829a834e6fe76711b1711d1d39158c627c74a2c632e91aa50fd222f9f65e2"
    )
    assert policies["g7c_integration_sha256"] == g7c.G7C_INTEGRATION_SHA256
    assert policies["target_chapter_range"] == [3, 24]
    assert policies["audit_first_reachable_chapter"] == 25
    assert policies["reasoning_policy"] == {
        "deepseek/deepseek-v4.1-flash": "low",
        "z-ai/glm-5.3-flash": "high",
        "qwen/qwen3.8-max-0902": "medium",
        "google/gemini-3.8-flash": "low",
        "tencent/hy3-paid": None,
        "xiaomi/mimo-v2.5-pro": None,
    }
    # Every active run carries its own frozen auxiliary reasoning binding.
    for record in bundle["runs"]:
        assert record["run_index"] in bundle["active_run_indices"]
        assert policies["auxiliary_reasoning_policy"][record["model_id"]]


def test_controller_never_scrapes_stdout_for_the_execution_result():
    source = MODULE_PATH.read_text(encoding="utf-8")
    # The only external process call is the read-only repository HEAD probe.
    assert source.count("subprocess.run(") == 1
    assert "readouterr" not in source
    assert "stdout.read" not in source
    assert "captured.out" not in source
    # Execution result and success proof come from the G7F return value plus the
    # durable progress/chapter/ledger artifacts only.
    assert "_verify_target_commit" in source
    assert "operator_authorized=True" in source


def test_execute_without_a_bundle_never_constructs_dpapi(tmp_path, monkeypatch):
    def forbidden():
        raise AssertionError("no DPAPI backend may be constructed")

    monkeypatch.setattr(g7r, "load_platform_dpapi", forbidden)

    with pytest.raises(g7r.G7RControllerError, match="authorize-paid-wave"):
        g7r.execute_wave(
            stage1_root=tmp_path,
            bundle_path=tmp_path / "missing.json",
            expected_bundle_sha256="a" * 64,
            authorize_paid_wave=False,
        )


# ---------------------------------------------------------------------------
# N. GPT-Sol review rework regressions
# ---------------------------------------------------------------------------


def test_reachable_glossary_review_maximum_is_four():
    assert g7r.OPERATION_SENDS_GLOSSARY_REVIEW == 4
    assert g7r.derive_glossary_review_send_ceiling() == 4

    primary = g7r.GLOSSARY_REVIEW_PRIMARY_SENDS
    repair = g7r.GLOSSARY_REVIEW_JSON_REPAIR_SENDS
    schema_branch = (
        primary + repair + g7r.GLOSSARY_REVIEW_REMOTE_SCHEMA_CORRECTION_SENDS
    )
    semantic_branch = (
        primary
        + repair
        + g7r.GLOSSARY_REVIEW_SEMANTIC_CORRECTION_SENDS
        + g7r.GLOSSARY_REVIEW_SEMANTIC_CORRECTION_RETRY_SENDS
    )
    assert schema_branch == 3
    assert semantic_branch == 4
    # The two correction branches are mutually exclusive: a durable remote schema
    # correction consumes the remote correction budget, so the reachable maximum
    # is the larger single branch, never their sum.
    assert g7r.derive_glossary_review_send_ceiling() == max(
        schema_branch, semantic_branch
    )
    assert g7r.derive_glossary_review_send_ceiling() < (
        schema_branch + semantic_branch
    )


def test_current_chapter3_wave_derives_per_run_sends_and_aggregate(
    tmp_path, monkeypatch
):
    root = _fixture_grid_root(tmp_path)
    _, bundle = _bundle_for(tmp_path, monkeypatch, chapter=3, root=root)

    assert bundle["active_run_indices"] == [3, 4, 5, 7, 8, 9, 10]
    sends = {
        record["run_index"]: record["maximum_new_provider_sends"]
        for record in bundle["runs"]
    }
    # Ace runs (residual-CJK disabled): 1 + 1 + 1 + 4 + 0 + 2 = 9.
    for index in (3, 5, 7, 9):
        assert sends[index] == 9, (index, sends[index])
    # Rogue runs (residual-CJK enabled): 1 + 1 + 1 + 4 + 0 + 4 = 11.
    for index in (4, 8, 10):
        assert sends[index] == 11, (index, sends[index])

    aggregate = bundle["ceilings"]["aggregate_maximum_new_provider_sends"]
    assert aggregate == 69
    assert aggregate == sum(sends.values())


def test_auto_resolution_optin_adds_the_bounded_adjudication_send(tmp_path):
    root = tmp_path / "stage1"
    run_root = _write_run(
        root,
        3,
        progress_last=2,
        novel_extra={
            "glossary_candidate_auto_resolution": {"enabled": True},
        },
    )
    _healthy_pair(run_root, SPECS[3])

    envelope = g7r.derive_run_envelope(root, SPECS[3], 3)

    assert envelope["glossary_candidate_auto_resolution_enabled"] is True
    assert envelope["operation_max_new_sends"]["glossary_review"] == 5
    assert envelope["operation_max_new_sends"]["terminology_repair"] == 2
    assert envelope["operation_max_new_sends"]["periodic_audit"] == 0
    assert envelope["total_max_new_sends"] == 1 + 1 + 1 + 5 + 0 + 2


def test_credit_equivalent_ceiling_prices_every_reachable_prompt(tmp_path):
    root = tmp_path / "stage1"
    run_root = _write_run(root, 1, progress_last=2)
    _healthy_pair(run_root, SPECS[1])

    envelope = g7r.derive_run_envelope(root, SPECS[1], 3)
    sends = envelope["total_max_new_sends"]
    bound = envelope["prompt_token_upper_bound_per_send"]
    rates = g7r._worst_case_rates(SPECS[1]["model_id"])
    assert rates["pricing_period"] == "peak"

    miss = Decimal(str(rates["cache_miss_rate_per_million"]))
    output = Decimal(str(rates["output_rate_per_million"]))
    completion = Decimal(
        g7r.TRANSLATION_OUTPUT_TOKENS
        + g7r.AUXILIARY_OUTPUT_TOKENS * (sends - 1)
    )

    expected = (
        Decimal(bound) * sends * miss + completion * output
    ) / Decimal(1_000_000)
    old_one_prompt = (
        Decimal(bound) * miss + completion * output
    ) / Decimal(1_000_000)

    assert Decimal(envelope["maximum_credit_equivalent_usd"]) == expected
    # The old one-prompt formula under-priced every additional send; the
    # corrected ceiling is exactly (sends - 1) extra priced prompts higher.
    assert old_one_prompt < expected
    assert expected - old_one_prompt == (
        Decimal(bound) * miss * (sends - 1)
    ) / Decimal(1_000_000)
    assert envelope["worst_case_pricing"]["prompt_input_priced_per_reachable_send"] is True
    assert envelope["worst_case_pricing"]["reachable_sends_priced"] == sends


def test_state_growth_budget_dominates_growth_beyond_64k(tmp_path):
    root = tmp_path / "stage1"
    run_root = _write_run(root, 1, progress_last=2)
    _healthy_pair(run_root, SPECS[1])

    envelope = g7r.derive_run_envelope(root, SPECS[1], 3)
    growth = envelope["prompt_headroom_bytes"]["glossary_and_state_growth_budget"]
    horizon = envelope["chapter_span"][1]
    max_source = envelope["max_source_characters_in_span"]

    # 64 KiB is not asserted as sufficient: the budget is a strictly derived,
    # finite, reproducible bound.
    assert growth > 65536
    assert growth == (
        (horizon - 1) * g7r.TRANSLATION_OUTPUT_TOKENS
        * g7r.UTF8_BYTES_PER_CHARACTER_UPPER_BOUND
        + horizon * g7r.AUXILIARY_OUTPUT_TOKENS
        * g7r.UTF8_BYTES_PER_CHARACTER_UPPER_BOUND
        + max_source * g7r.UTF8_BYTES_PER_CHARACTER_UPPER_BOUND
    )

    # A synthetic serialized request whose glossary/continuity state alone grows
    # well beyond 64 KiB must still be dominated by the envelope.
    growing_text = "字" * 200_000
    body = {
        "model": "fixture",
        "messages": [{"role": "user", "content": growing_text}],
        "schema_scaffold": "x" * 4096,
        "glossary": ["term"] * 2000,
    }
    serialized = len(
        json.dumps(
            body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    )
    assert serialized > 65536
    g7f_prompt_bound = serialized + g7r.PROMPT_TOKEN_SAFETY_OVERHEAD
    assert envelope["prompt_token_upper_bound_per_send"] >= g7f_prompt_bound


def test_all_terminal_wave_completes_without_stopping(tmp_path, monkeypatch):
    root = tmp_path / "stage1"
    for index in (3, 4):
        run_root = _write_run(root, index, progress_last=2)
        _healthy_pair(run_root, SPECS[index])

    bundle_path, bundle = _bundle_for(
        tmp_path, monkeypatch, chapter=3, root=root
    )
    run_root_of = {
        item["run_index"]: Path(item["run_root"]) for item in bundle["runs"]
    }
    created: dict[int, "g7f.InvocationBudgetLedger"] = {}

    def handler(self, kwargs):
        index = kwargs["plan"]["run_index"]
        # Every durable terminal response crossed the paid boundary: its target
        # ledger records the started send.
        created[index] = _canonical_ledger(
            run_root_of[index],
            kwargs["plan"],
            sends=1,
            accepted_usage=(1000, 500),
        )
        _terminal_auxiliary_journal(
            run_root_of[index],
            "0003.g7c_state_extraction.json",
            operation_kind="state_extraction",
            chapter=3,
            spec=SPECS[index],
        )
        raise g7f.G7FExecutionError("definitive durable terminal outcome")

    dpapi = FakeDpapi()
    executor = FakeExecutor(handler=handler)
    result = _execute(
        tmp_path,
        bundle_path=bundle_path,
        bundle=bundle,
        dpapi=dpapi,
        preflight=FakePreflight(),
        executor=executor,
    )

    assert result["status"] == g7r.WAVE_COMPLETED_WITH_TERMINAL_RUNS
    assert result["fail_closed"] is False
    assert result["stop_reason"] is None
    assert result["completed_set"] == []
    assert result["newly_terminal_set"] == [3, 4]
    assert result["newly_quarantined_set"] == []
    assert result["not_started_after_stop_set"] == []
    assert result["remaining_active_set_for_target"] == []
    assert result["next_active_set"] == []
    # Every selected run is processed exactly once; no retries.
    assert [call["plan"]["run_index"] for call in executor.calls] == [3, 4]
    # Each terminal response is a real paid send recorded in its target ledger.
    assert result["accounting_complete"] is True
    assert result["actual_new_provider_sends"] == 2
    assert Decimal(result["accepted_credit_equivalent_usd"]) == (
        created[3].accepted_cost + created[4].accepted_cost
    )


def test_terminal_and_completed_accounting_reconciles_with_durable_ledgers(
    tmp_path, monkeypatch
):
    root = tmp_path / "stage1"
    for index in (3, 4):
        run_root = _write_run(root, index, progress_last=2)
        _healthy_pair(run_root, SPECS[index])

    bundle_path, bundle = _bundle_for(
        tmp_path, monkeypatch, chapter=3, root=root
    )
    run_root_of = {
        item["run_index"]: Path(item["run_root"]) for item in bundle["runs"]
    }
    created: dict[int, "g7f.InvocationBudgetLedger"] = {}

    def handler(self, kwargs):
        index = kwargs["plan"]["run_index"]
        if index == 3:
            created[3] = _canonical_ledger(
                run_root_of[3],
                kwargs["plan"],
                sends=2,
                accepted_usage=(1500, 1000),
            )
            _terminal_auxiliary_journal(
                run_root_of[3],
                "0003.g7c_state_extraction.json",
                operation_kind="state_extraction",
                chapter=3,
                spec=SPECS[3],
            )
            raise g7f.G7FExecutionError("terminal with accepted cost")
        _simulate_success(
            run_root_of[4], 3, kwargs["plan"], accepted_usage=(1000, 200)
        )
        created[4] = g7f.InvocationBudgetLedger.open_or_create(
            run_root=run_root_of[4],
            plan=kwargs["plan"],
            clock=lambda: NOW,
            allow_create=False,
        )
        return {"status": "COMPLETED"}

    dpapi = FakeDpapi()
    result = _execute(
        tmp_path,
        bundle_path=bundle_path,
        bundle=bundle,
        dpapi=dpapi,
        preflight=FakePreflight(),
        executor=FakeExecutor(handler=handler),
    )

    terminal_cost = created[3].accepted_cost
    completed_cost = created[4].accepted_cost
    assert result["status"] == g7r.WAVE_COMPLETED_WITH_TERMINAL_RUNS
    assert result["fail_closed"] is False
    assert result["completed_set"] == [4]
    assert result["newly_terminal_set"] == [3]

    assert result["actual_new_provider_sends"] == 3
    assert Decimal(result["accepted_credit_equivalent_usd"]) == (
        terminal_cost + completed_cost
    )
    sends_by_disposition = result["actual_new_provider_sends_by_disposition"]
    accepted_by_disposition = result[
        "accepted_credit_equivalent_usd_by_disposition"
    ]
    assert sends_by_disposition == {
        g7r.RUN_OUTCOME_TERMINAL: 2,
        g7r.RUN_OUTCOME_COMPLETED: 1,
    }
    assert Decimal(accepted_by_disposition[g7r.RUN_OUTCOME_TERMINAL]) == (
        terminal_cost
    )
    assert Decimal(accepted_by_disposition[g7r.RUN_OUTCOME_COMPLETED]) == (
        completed_cost
    )

    # completed + terminal accounting reconciles exactly with the durable
    # target ledgers.
    reconciled_sends = 0
    reconciled_cost = Decimal(0)
    for line in result["actual_accounting_lines"]:
        assert line["ledger_present"] is True
        assert Path(line["ledger_path"]).is_file()
        reconciled_sends += line["new_provider_sends_started"]
        reconciled_cost += Decimal(line["accepted_credit_equivalent_usd"])
    assert reconciled_sends == result["actual_new_provider_sends"] == 3
    assert reconciled_cost == Decimal(result["accepted_credit_equivalent_usd"])
    assert reconciled_cost == terminal_cost + completed_cost
    assert sum(sends_by_disposition.values()) == 3
    assert sum(
        Decimal(value) for value in accepted_by_disposition.values()
    ) == terminal_cost + completed_cost


def test_plan_records_actual_checkout_head_when_expected_omitted(
    tmp_path, monkeypatch
):
    root = _all_ready_root(tmp_path)
    _patch_run_binding(monkeypatch)

    bundle = g7r.plan_wave(
        stage1_root=root,
        target_chapter=3,
        control_dir=tmp_path / "control",
    )

    assert bundle["repository_head"] == g7r.resolve_repository_head(REPO_ROOT)


def test_plan_with_matching_expected_head_succeeds(tmp_path, monkeypatch):
    root = _all_ready_root(tmp_path)
    _patch_run_binding(monkeypatch)
    actual = g7r.resolve_repository_head(REPO_ROOT)

    bundle = g7r.plan_wave(
        stage1_root=root,
        target_chapter=3,
        control_dir=tmp_path / "control",
        repository_head=actual,
    )

    assert bundle["repository_head"] == actual


def test_plan_with_mismatched_expected_head_fails_before_finalization(
    tmp_path, monkeypatch
):
    root = _all_ready_root(tmp_path)
    _patch_run_binding(monkeypatch)

    with pytest.raises(g7r.G7RBundleError, match="does not match"):
        g7r.plan_wave(
            stage1_root=root,
            target_chapter=3,
            control_dir=tmp_path / "control",
            repository_head="f" * 40,
        )

    assert not g7r.wave_bundle_path(tmp_path / "control", 3).exists()


def test_plan_head_change_during_planning_fails_closed(tmp_path, monkeypatch):
    root = _all_ready_root(tmp_path)
    _patch_run_binding(monkeypatch)
    calls = {"count": 0}

    def resolver(_root):
        calls["count"] += 1
        return "a" * 40 if calls["count"] == 1 else "b" * 40

    with pytest.raises(g7r.G7RBundleError, match="changed during"):
        g7r.plan_wave(
            stage1_root=root,
            target_chapter=3,
            control_dir=tmp_path / "control",
            repository_head_resolver=resolver,
        )

    assert calls["count"] == 2
    assert not g7r.wave_bundle_path(tmp_path / "control", 3).exists()


# ---------------------------------------------------------------------------
# O. Final GPT-Sol corrupt-ledger / truthful-reporting hardenings
#
# These are the regressions for the three final review blockers:
#   A. a terminal response requires a valid target G7F ledger;
#   B. a corrupt/missing ledger must never crash post-wave accounting and must
#      never be fabricated as zero;
#   C. the CLI must never emit false zero-Provider/credential-free evidence once
#      the paid boundary setup has begun.
# ---------------------------------------------------------------------------


def _ready_single_root(base: Path) -> Path:
    root = base / "stage1"
    run_root = _write_run(root, 3, progress_last=2)
    _healthy_pair(run_root, SPECS[3])
    return root


def _two_ready_runs(tmp_path: Path) -> Path:
    root = tmp_path / "stage1"
    for index in (3, 4):
        run_root = _write_run(root, index, progress_last=2)
        _healthy_pair(run_root, SPECS[index])
    return root


def _run_main_execute(
    monkeypatch,
    *,
    bundle_path,
    bundle,
    dpapi,
    preflight,
    executor,
    secret_path=None,
    progress_log=None,
):
    """Drive the real CLI execute path with fully injected fakes."""
    monkeypatch.setattr(g7r, "load_platform_dpapi", lambda: dpapi)
    monkeypatch.setattr(g7r, "run_preflight", preflight)
    monkeypatch.setattr(g7r, "execute_stage1_invocation", executor)
    args = [
        "execute",
        "--stage1-root",
        bundle["stage1_root"],
        "--bundle",
        str(bundle_path),
        "--expected-bundle-sha256",
        bundle["bundle_sha256"],
        "--authorize-paid-wave",
    ]
    if secret_path is not None:
        args += ["--secret-path", str(secret_path)]
    if progress_log is not None:
        args += ["--progress-log", str(progress_log)]
    return g7r.main(args)


def test_terminal_with_missing_ledger_quarantines_and_accounting_is_incomplete(
    tmp_path, monkeypatch
):
    root = _two_ready_runs(tmp_path)
    bundle_path, bundle = _bundle_for(tmp_path, monkeypatch, chapter=3, root=root)
    run_root_of = {
        item["run_index"]: Path(item["run_root"]) for item in bundle["runs"]
    }
    untouched = _tree_hash(run_root_of[4])

    def handler(self, kwargs):
        index = kwargs["plan"]["run_index"]
        # A durable terminal response whose canonical target ledger is MISSING.
        _terminal_auxiliary_journal(
            run_root_of[index],
            "0003.g7c_state_extraction.json",
            operation_kind="state_extraction",
            chapter=3,
            spec=SPECS[index],
        )
        raise g7f.G7FExecutionError("terminal response without a target ledger")

    executor = FakeExecutor(handler=handler)
    result = _execute(
        tmp_path,
        bundle_path=bundle_path,
        bundle=bundle,
        dpapi=FakeDpapi(),
        preflight=FakePreflight(),
        executor=executor,
    )

    # The response_recorded terminal evidence is not trusted without its ledger:
    # the line is corrupt/unknown durable state, quarantined, and the wave stops.
    assert result["per_run_results"][0]["outcome"] == (
        g7r.RUN_OUTCOME_CORRUPT_STATE
    )
    assert result["newly_quarantined_set"] == [3]
    assert result["newly_terminal_set"] == []
    assert result["status"] == g7r.WAVE_STOPPED_AMBIGUOUS
    assert result["fail_closed"] is True
    assert result["not_started_after_stop_set"] == [4]
    # No retry, and the later run is byte-identical untouched.
    assert [call["plan"]["run_index"] for call in executor.calls] == [3]
    assert _tree_hash(run_root_of[4]) == untouched
    # Accounting is incomplete: no fabricated zero, exact aggregates unknown.
    assert result["accounting_complete"] is False
    assert result["actual_new_provider_sends"] is None
    assert result["accepted_credit_equivalent_usd"] is None
    assert result["accounting_unavailable_run_indices"] == [3]
    assert result["known_accounted_new_provider_sends"] == 0
    assert result["known_accounted_credit_equivalent_usd"] == "0"
    assert result["actual_accounting_lines"][0]["status"] == (
        g7r.ACCOUNTING_UNAVAILABLE
    )


def test_terminal_with_corrupt_ledger_quarantines_the_same_way(tmp_path, monkeypatch):
    root = _two_ready_runs(tmp_path)
    bundle_path, bundle = _bundle_for(tmp_path, monkeypatch, chapter=3, root=root)
    run_root_of = {
        item["run_index"]: Path(item["run_root"]) for item in bundle["runs"]
    }
    untouched = _tree_hash(run_root_of[4])

    def handler(self, kwargs):
        index = kwargs["plan"]["run_index"]
        _write_corrupt_ledger(run_root_of[index], 3)
        _terminal_auxiliary_journal(
            run_root_of[index],
            "0003.g7c_state_extraction.json",
            operation_kind="state_extraction",
            chapter=3,
            spec=SPECS[index],
        )
        raise g7f.G7FExecutionError("terminal response with a corrupt ledger")

    executor = FakeExecutor(handler=handler)
    result = _execute(
        tmp_path,
        bundle_path=bundle_path,
        bundle=bundle,
        dpapi=FakeDpapi(),
        preflight=FakePreflight(),
        executor=executor,
    )

    assert result["per_run_results"][0]["outcome"] == (
        g7r.RUN_OUTCOME_CORRUPT_STATE
    )
    assert result["newly_quarantined_set"] == [3]
    assert result["newly_terminal_set"] == []
    assert result["status"] == g7r.WAVE_STOPPED_AMBIGUOUS
    assert result["fail_closed"] is True
    assert result["not_started_after_stop_set"] == [4]
    assert [call["plan"]["run_index"] for call in executor.calls] == [3]
    assert _tree_hash(run_root_of[4]) == untouched
    assert result["accounting_complete"] is False
    assert result["actual_new_provider_sends"] is None
    assert result["accepted_credit_equivalent_usd"] is None
    assert result["accounting_unavailable_run_indices"] == [3]
    assert result["actual_accounting_lines"][0]["status"] == (
        g7r.ACCOUNTING_UNAVAILABLE
    )


def test_send_like_journal_with_corrupt_ledger_does_not_crash_accounting(
    tmp_path, monkeypatch
):
    root = _two_ready_runs(tmp_path)
    bundle_path, bundle = _bundle_for(tmp_path, monkeypatch, chapter=3, root=root)
    run_root_of = {
        item["run_index"]: Path(item["run_root"]) for item in bundle["runs"]
    }

    def handler(self, kwargs):
        index = kwargs["plan"]["run_index"]
        _write_corrupt_ledger(run_root_of[index], 3)
        _write_journal(
            run_root_of[index],
            "0003.g7c_translation.json",
            _journal_record(
                status="started",
                operation_kind="translation",
                chapter=3,
                model=SPECS[index]["model_id"],
                novel_id=SPECS[index]["novel_id"],
            ),
        )
        raise g7f.G7FExecutionError("ambiguous start alongside a corrupt ledger")

    executor = FakeExecutor(handler=handler)
    result = _execute(
        tmp_path,
        bundle_path=bundle_path,
        bundle=bundle,
        dpapi=FakeDpapi(),
        preflight=FakePreflight(),
        executor=executor,
    )

    # The structured quarantine result survives the corrupt ledger.
    assert result["status"] == g7r.WAVE_STOPPED_AMBIGUOUS
    assert result["fail_closed"] is True
    assert result["newly_quarantined_set"] == [3]
    assert result["per_run_results"][0]["outcome"] == g7r.RUN_OUTCOME_QUARANTINED
    assert result["not_started_after_stop_set"] == [4]
    assert result["accounting_complete"] is False
    assert result["actual_new_provider_sends"] is None
    assert result["accepted_credit_equivalent_usd"] is None
    assert result["accounting_unavailable_run_indices"] == [3]


def test_valid_ledger_terminal_stays_terminal_and_continues(tmp_path, monkeypatch):
    root = _two_ready_runs(tmp_path)
    bundle_path, bundle = _bundle_for(tmp_path, monkeypatch, chapter=3, root=root)
    run_root_of = {
        item["run_index"]: Path(item["run_root"]) for item in bundle["runs"]
    }
    created: dict[int, "g7f.InvocationBudgetLedger"] = {}

    def handler(self, kwargs):
        index = kwargs["plan"]["run_index"]
        if index == 3:
            created[3] = _canonical_ledger(
                run_root_of[3],
                kwargs["plan"],
                sends=1,
                accepted_usage=(2000, 1000),
            )
            _terminal_auxiliary_journal(
                run_root_of[3],
                "0003.g7c_state_extraction.json",
                operation_kind="state_extraction",
                chapter=3,
                spec=SPECS[3],
            )
            raise g7f.G7FExecutionError("proven durable terminal outcome")
        _simulate_success(run_root_of[4], 3, kwargs["plan"])
        return {"status": "COMPLETED"}

    executor = FakeExecutor(handler=handler)
    result = _execute(
        tmp_path,
        bundle_path=bundle_path,
        bundle=bundle,
        dpapi=FakeDpapi(),
        preflight=FakePreflight(),
        executor=executor,
    )

    assert result["newly_terminal_set"] == [3]
    assert result["completed_set"] == [4]
    assert result["newly_quarantined_set"] == []
    assert result["status"] == g7r.WAVE_COMPLETED_WITH_TERMINAL_RUNS
    assert result["fail_closed"] is False
    assert [call["plan"]["run_index"] for call in executor.calls] == [3, 4]
    # Exact terminal accounting is included in the complete wave totals.
    assert result["accounting_complete"] is True
    assert result["actual_new_provider_sends"] == 2
    assert result["accounting_unavailable_run_indices"] == []
    assert result["actual_new_provider_sends_by_disposition"][
        g7r.RUN_OUTCOME_TERMINAL
    ] == 1
    assert Decimal(
        result["accepted_credit_equivalent_usd_by_disposition"][
            g7r.RUN_OUTCOME_TERMINAL
        ]
    ) == created[3].accepted_cost


def test_pre_send_local_failure_is_exact_zero_and_complete(tmp_path, monkeypatch):
    root = _two_ready_runs(tmp_path)
    bundle_path, bundle = _bundle_for(tmp_path, monkeypatch, chapter=3, root=root)
    untouched = _tree_hash(Path(bundle["runs"][1]["run_root"]))

    executor = FakeExecutor(
        handler=lambda self, kwargs: (_ for _ in ()).throw(
            g7f.G7FExecutionError("local failure before any send")
        )
    )
    result = _execute(
        tmp_path,
        bundle_path=bundle_path,
        bundle=bundle,
        dpapi=FakeDpapi(),
        preflight=FakePreflight(),
        executor=executor,
    )

    assert result["status"] == g7r.WAVE_STOPPED_LOCAL_FAILURE
    assert result["fail_closed"] is True
    assert result["per_run_results"][0]["outcome"] == g7r.RUN_OUTCOME_LOCAL_FAILURE
    assert result["newly_quarantined_set"] == []
    assert _tree_hash(Path(bundle["runs"][1]["run_root"])) == untouched
    # A proven pre-send failure with no boundary crossing stays exact zero and
    # keeps the accounting complete.
    assert result["accounting_complete"] is True
    assert result["actual_new_provider_sends"] == 0
    assert Decimal(result["accepted_credit_equivalent_usd"]) == Decimal("0")
    assert result["known_accounted_new_provider_sends"] == 0
    assert result["accounting_unavailable_run_indices"] == []
    assert result["actual_accounting_lines"][0]["status"] == (
        g7r.ACCOUNTING_ZERO_PROVEN_PRE_SEND
    )


def test_healthy_wave_preserves_exact_accounting_and_result_shape(
    tmp_path, monkeypatch
):
    root = _two_ready_runs(tmp_path)
    bundle_path, bundle = _bundle_for(tmp_path, monkeypatch, chapter=3, root=root)
    run_root_of = {
        item["run_index"]: Path(item["run_root"]) for item in bundle["runs"]
    }
    created: dict[int, "g7f.InvocationBudgetLedger"] = {}

    def handler(self, kwargs):
        index = kwargs["plan"]["run_index"]
        _simulate_success(
            run_root_of[index], 3, kwargs["plan"], accepted_usage=(1000, 200)
        )
        created[index] = g7f.InvocationBudgetLedger.open_or_create(
            run_root=run_root_of[index],
            plan=kwargs["plan"],
            clock=lambda: NOW,
            allow_create=False,
        )
        return {"status": "COMPLETED"}

    result = _execute(
        tmp_path,
        bundle_path=bundle_path,
        bundle=bundle,
        dpapi=FakeDpapi(),
        preflight=FakePreflight(),
        executor=FakeExecutor(handler=handler),
    )

    for field in (
        "status",
        "fail_closed",
        "stop_reason",
        "starting_active_set",
        "per_run_results",
        "completed_set",
        "newly_terminal_set",
        "newly_quarantined_set",
        "not_started_after_stop_set",
        "remaining_active_set_for_target",
        "actual_accounting_lines",
        "original_ceilings",
        "next_active_set",
        "next_chapter",
        "authorization",
    ):
        assert field in result
    assert result["status"] == g7r.WAVE_COMPLETED
    assert result["completed_set"] == [3, 4]
    assert result["accounting_complete"] is True
    assert result["actual_new_provider_sends"] == 2
    assert result["known_accounted_new_provider_sends"] == 2
    expected_cost = created[3].accepted_cost + created[4].accepted_cost
    assert Decimal(result["accepted_credit_equivalent_usd"]) == expected_cost
    assert Decimal(result["known_accounted_credit_equivalent_usd"]) == (
        expected_cost
    )
    assert result["accounting_unavailable_run_indices"] == []
    for line in result["actual_accounting_lines"]:
        assert line["status"] == g7r.ACCOUNTING_EXACT


def test_secret_path_missing_fails_before_dpapi_construction(tmp_path, monkeypatch):
    root = _ready_single_root(tmp_path)
    bundle_path, bundle = _bundle_for(
        tmp_path, monkeypatch, chapter=3, root=root
    )

    def forbidden():
        raise AssertionError(
            "DPAPI must not be constructed before --secret-path validation"
        )

    monkeypatch.setattr(g7r, "load_platform_dpapi", forbidden)
    executor = FakeExecutor(
        handler=lambda self, kwargs: pytest.fail("no run may execute")
    )

    with pytest.raises(g7r.G7RControllerError) as info:
        g7r.execute_wave(
            stage1_root=bundle["stage1_root"],
            bundle_path=bundle_path,
            expected_bundle_sha256=bundle["bundle_sha256"],
            authorize_paid_wave=True,
            secret_path=None,
            dpapi=None,
            preflight_fn=FakePreflight(),
            execute_fn=executor,
            repository_head_resolver=lambda _root: bundle["repository_head"],
            clock=lambda: NOW,
        )

    assert "--secret-path" in str(info.value)
    # A pre-credential controller error, never a paid-phase error.
    assert not isinstance(info.value, g7r.G7RExecutionPhaseError)
    assert executor.calls == []


def test_unprotect_failure_reports_attempted_access_and_zero_calls(
    tmp_path, monkeypatch, capsys
):
    root = _ready_single_root(tmp_path)
    head = g7r.resolve_repository_head(REPO_ROOT)
    bundle_path, bundle = _bundle_for(
        tmp_path, monkeypatch, chapter=3, root=root, head=head
    )
    dpapi = FakeDpapi(fail_unprotect=True)
    secret_path = _write_secret(tmp_path, dpapi)

    with pytest.raises(g7r.G7RExecutionPhaseError) as info:
        _execute(
            tmp_path,
            bundle_path=bundle_path,
            bundle=bundle,
            dpapi=dpapi,
            preflight=FakePreflight(),
            executor=FakeExecutor(
                handler=lambda self, kwargs: pytest.fail("no send")
            ),
            secret_path=secret_path,
        )
    error = info.value
    assert error.phase == g7r.EXECUTION_PHASE_SECRET_ACCESS
    assert error.provider_calls == 0
    assert error.credential_access is False
    assert error.credential_access_attempted is True
    assert dpapi.unprotect_calls  # the decrypt was attempted

    code = _run_main_execute(
        monkeypatch,
        bundle_path=bundle_path,
        bundle=bundle,
        dpapi=FakeDpapi(fail_unprotect=True),
        preflight=FakePreflight(),
        executor=FakeExecutor(handler=lambda self, kwargs: pytest.fail("no send")),
        secret_path=secret_path,
    )
    document = json.loads(capsys.readouterr().out)
    assert code == 1
    assert document["provider_calls"] == 0
    assert document["credential_access"] is False
    assert document["credential_access_attempted"] is True
    assert document["phase"] == g7r.EXECUTION_PHASE_SECRET_ACCESS


def test_progress_log_open_failure_reports_truthful_post_secret_metadata(
    tmp_path, monkeypatch, capsys
):
    root = _ready_single_root(tmp_path)
    head = g7r.resolve_repository_head(REPO_ROOT)
    bundle_path, bundle = _bundle_for(
        tmp_path, monkeypatch, chapter=3, root=root, head=head
    )
    dpapi = FakeDpapi()
    secret_path = _write_secret(tmp_path, dpapi)
    blocker = tmp_path / "not_a_directory"
    blocker.write_text("file", encoding="utf-8")
    bad_log = blocker / "progress.log"

    with pytest.raises(g7r.G7RExecutionPhaseError) as info:
        _execute(
            tmp_path,
            bundle_path=bundle_path,
            bundle=bundle,
            dpapi=dpapi,
            preflight=FakePreflight(),
            executor=FakeExecutor(
                handler=lambda self, kwargs: pytest.fail("no send")
            ),
            secret_path=secret_path,
            progress_log_path=bad_log,
        )
    error = info.value
    assert error.phase == g7r.EXECUTION_PHASE_POST_SECRET_PRE_EXECUTION
    assert error.provider_calls == 0
    assert error.credential_access is True
    assert error.credential_access_attempted is True
    assert dpapi.unprotect_calls  # the secret was successfully accessed

    code = _run_main_execute(
        monkeypatch,
        bundle_path=bundle_path,
        bundle=bundle,
        dpapi=FakeDpapi(),
        preflight=FakePreflight(),
        executor=FakeExecutor(handler=lambda self, kwargs: pytest.fail("no send")),
        secret_path=secret_path,
        progress_log=bad_log,
    )
    document = json.loads(capsys.readouterr().out)
    assert code == 1
    assert document["provider_calls"] == 0
    assert document["credential_access"] is True
    assert document["phase"] == g7r.EXECUTION_PHASE_POST_SECRET_PRE_EXECUTION


def test_unexpected_post_send_accounting_failure_never_claims_zero_provider(
    tmp_path, monkeypatch, capsys
):
    head = g7r.resolve_repository_head(REPO_ROOT)

    # Scenario A: the real execute_wave path with an exploding accounting reader.
    root_a = _ready_single_root(tmp_path / "a")
    bundle_path_a, bundle_a = _bundle_for(
        tmp_path / "a", monkeypatch, chapter=3, root=root_a, head=head
    )
    secret_a = _write_secret(tmp_path / "a", FakeDpapi())

    def handler(self, kwargs):
        _simulate_success(
            Path(bundle_a["runs"][0]["run_root"]), 3, kwargs["plan"]
        )
        return {"status": "COMPLETED"}

    def explode(*_args, **_kwargs):
        raise RuntimeError("synthetic post-send accounting explosion")

    monkeypatch.setattr(g7r, "_accounting_record_for_line", explode)

    with pytest.raises(g7r.G7RExecutionPhaseError) as info:
        g7r.execute_wave(
            stage1_root=bundle_a["stage1_root"],
            bundle_path=bundle_path_a,
            expected_bundle_sha256=bundle_a["bundle_sha256"],
            authorize_paid_wave=True,
            secret_path=secret_a,
            dpapi=FakeDpapi(),
            preflight_fn=FakePreflight(),
            execute_fn=FakeExecutor(handler=handler),
            repository_head_resolver=lambda _root: head,
            clock=lambda: NOW,
        )
    error = info.value
    assert error.phase == g7r.EXECUTION_PHASE_POST_SECRET_EXECUTION
    # Unknown, never a fabricated zero.
    assert error.provider_calls is None
    assert error.provider_calls != 0
    assert error.credential_access is True
    assert error.credential_access_attempted is True

    # Scenario B: the same failure reported through the real CLI.
    root_b = _ready_single_root(tmp_path / "b")
    bundle_path_b, bundle_b = _bundle_for(
        tmp_path / "b", monkeypatch, chapter=3, root=root_b, head=head
    )
    secret_b = _write_secret(tmp_path / "b", FakeDpapi())

    def handler_b(self, kwargs):
        _simulate_success(
            Path(bundle_b["runs"][0]["run_root"]), 3, kwargs["plan"]
        )
        return {"status": "COMPLETED"}

    code = _run_main_execute(
        monkeypatch,
        bundle_path=bundle_path_b,
        bundle=bundle_b,
        dpapi=FakeDpapi(),
        preflight=FakePreflight(),
        executor=FakeExecutor(handler=handler_b),
        secret_path=secret_b,
    )
    document = json.loads(capsys.readouterr().out)
    assert code == 1
    assert document["provider_calls"] is None
    assert document["provider_calls"] != 0
    assert document["credential_access"] is True
    assert document["phase"] == g7r.EXECUTION_PHASE_POST_SECRET_EXECUTION


# ---------------------------------------------------------------------------
# P. Canonical frozen-G7F ledger validation
#
# G7R must not independently weaken the frozen G7F InvocationBudgetLedger
# contract.  Every durable target ledger is validated through the REAL frozen
# ``InvocationBudgetLedger.open_or_create(..., allow_create=False)`` contract
# (schema, revision, ledger_sha256, plan/approval binding, records, send and
# accepted-cost ceilings) against the exact materialized plan, so a JSON-
# readable ledger with a stale hash, a wrong plan/approval binding, inconsistent
# records, or a wrong run/chapter identity is never admissible terminal or
# accounting evidence.
# ---------------------------------------------------------------------------


def _plan_for(bundle_path: Path, bundle, run_index: int):
    record = next(
        item for item in bundle["runs"] if item["run_index"] == run_index
    )
    return json.loads(
        (bundle_path.parent / record["plan_relative_path"]).read_text(
            encoding="utf-8"
        )
    )


def _tamper_ledger(run_root: Path, plan, mutate) -> Path:
    """Tamper a canonical ledger, keeping its ledger_sha256 self-consistent."""
    ledger = g7f.InvocationBudgetLedger.open_or_create(
        run_root=run_root, plan=plan, clock=lambda: NOW, allow_create=False
    )
    mutate(ledger.document)
    ledger._persist()
    return ledger.path


def _stale_hash_ledger(run_root: Path, plan, mutate) -> Path:
    """Tamper a canonical ledger on disk WITHOUT recomputing ledger_sha256."""
    path = g7f.invocation_ledger_path(run_root, plan["target_chapter"])
    document = json.loads(path.read_text(encoding="utf-8"))
    mutate(document)
    path.write_text(
        json.dumps(document, indent=2, sort_keys=True), encoding="utf-8"
    )
    return path


def _terminal_aux(run_root: Path, run_index: int) -> None:
    _terminal_auxiliary_journal(
        run_root,
        "0003.g7c_state_extraction.json",
        operation_kind="state_extraction",
        chapter=3,
        spec=SPECS[run_index],
    )


def test_canonical_valid_terminal_ledger_continues_and_accounts_exactly(
    tmp_path, monkeypatch
):
    root = _two_ready_runs(tmp_path)
    bundle_path, bundle = _bundle_for(tmp_path, monkeypatch, chapter=3, root=root)
    run_root_of = {
        item["run_index"]: Path(item["run_root"]) for item in bundle["runs"]
    }
    created: dict[int, "g7f.InvocationBudgetLedger"] = {}

    def handler(self, kwargs):
        index = kwargs["plan"]["run_index"]
        if index == 3:
            created[3] = _canonical_ledger(
                run_root_of[3],
                kwargs["plan"],
                sends=1,
                accepted_usage=(1000, 500),
            )
            _terminal_aux(run_root_of[3], 3)
            raise g7f.G7FExecutionError("canonical durable terminal outcome")
        _simulate_success(
            run_root_of[4], 3, kwargs["plan"], accepted_usage=(800, 250)
        )
        created[4] = g7f.InvocationBudgetLedger.open_or_create(
            run_root=run_root_of[4],
            plan=kwargs["plan"],
            clock=lambda: NOW,
            allow_create=False,
        )
        return {"status": "COMPLETED"}

    result = _execute(
        tmp_path,
        bundle_path=bundle_path,
        bundle=bundle,
        dpapi=FakeDpapi(),
        preflight=FakePreflight(),
        executor=FakeExecutor(handler=handler),
    )

    assert result["newly_terminal_set"] == [3]
    assert result["completed_set"] == [4]
    assert result["newly_quarantined_set"] == []
    assert result["status"] == g7r.WAVE_COMPLETED_WITH_TERMINAL_RUNS
    assert result["fail_closed"] is False
    assert result["accounting_complete"] is True
    assert result["accounting_unavailable_run_indices"] == []
    assert result["actual_new_provider_sends"] == 2
    assert all(
        line["status"] == g7r.ACCOUNTING_EXACT
        for line in result["actual_accounting_lines"]
    )
    assert Decimal(result["accepted_credit_equivalent_usd"]) == (
        created[3].accepted_cost + created[4].accepted_cost
    )


def test_stale_ledger_sha256_is_corrupt_and_quarantines(tmp_path, monkeypatch):
    root = _two_ready_runs(tmp_path)
    bundle_path, bundle = _bundle_for(tmp_path, monkeypatch, chapter=3, root=root)
    run_root_of = {
        item["run_index"]: Path(item["run_root"]) for item in bundle["runs"]
    }
    untouched = _tree_hash(run_root_of[4])

    def handler(self, kwargs):
        _canonical_ledger(run_root_of[3], kwargs["plan"], sends=1)

        def mutate(document):
            document["new_provider_sends_started"] += 1

        _stale_hash_ledger(run_root_of[3], kwargs["plan"], mutate)
        _terminal_aux(run_root_of[3], 3)
        raise g7f.G7FExecutionError("terminal with a stale-hash ledger")

    result = _execute(
        tmp_path,
        bundle_path=bundle_path,
        bundle=bundle,
        dpapi=FakeDpapi(),
        preflight=FakePreflight(),
        executor=FakeExecutor(handler=handler),
    )

    assert result["per_run_results"][0]["outcome"] == (
        g7r.RUN_OUTCOME_CORRUPT_STATE
    )
    assert result["newly_terminal_set"] == []
    assert result["newly_quarantined_set"] == [3]
    assert result["status"] == g7r.WAVE_STOPPED_AMBIGUOUS
    assert result["fail_closed"] is True
    assert result["not_started_after_stop_set"] == [4]
    assert _tree_hash(run_root_of[4]) == untouched
    assert result["accounting_complete"] is False
    assert result["actual_new_provider_sends"] is None
    assert result["accepted_credit_equivalent_usd"] is None
    assert result["accounting_unavailable_run_indices"] == [3]
    assert result["actual_accounting_lines"][0]["status"] == (
        g7r.ACCOUNTING_UNAVAILABLE
    )


@pytest.mark.parametrize("field", ["plan_sha256", "approval_token"])
def test_ledger_bound_to_another_plan_is_corrupt(tmp_path, monkeypatch, field):
    root = _two_ready_runs(tmp_path)
    bundle_path, bundle = _bundle_for(tmp_path, monkeypatch, chapter=3, root=root)
    run_root_of = {
        item["run_index"]: Path(item["run_root"]) for item in bundle["runs"]
    }
    untouched = _tree_hash(run_root_of[4])

    def handler(self, kwargs):
        _canonical_ledger(run_root_of[3], kwargs["plan"], sends=1)

        def mutate(document):
            document[field] = "9" * 64

        _tamper_ledger(run_root_of[3], kwargs["plan"], mutate)
        _terminal_aux(run_root_of[3], 3)
        raise g7f.G7FExecutionError("terminal with a mis-bound ledger")

    result = _execute(
        tmp_path,
        bundle_path=bundle_path,
        bundle=bundle,
        dpapi=FakeDpapi(),
        preflight=FakePreflight(),
        executor=FakeExecutor(handler=handler),
    )

    assert result["per_run_results"][0]["outcome"] == (
        g7r.RUN_OUTCOME_CORRUPT_STATE
    )
    assert result["newly_terminal_set"] == []
    assert result["newly_quarantined_set"] == [3]
    assert result["status"] == g7r.WAVE_STOPPED_AMBIGUOUS
    assert result["fail_closed"] is True
    assert result["not_started_after_stop_set"] == [4]
    assert _tree_hash(run_root_of[4]) == untouched
    assert result["accounting_complete"] is False
    assert result["actual_new_provider_sends"] is None
    assert result["accounting_unavailable_run_indices"] == [3]


def test_ledger_send_count_disagreeing_with_records_is_corrupt(
    tmp_path, monkeypatch
):
    root = _two_ready_runs(tmp_path)
    bundle_path, bundle = _bundle_for(tmp_path, monkeypatch, chapter=3, root=root)
    run_root_of = {
        item["run_index"]: Path(item["run_root"]) for item in bundle["runs"]
    }
    untouched = _tree_hash(run_root_of[4])

    def handler(self, kwargs):
        _canonical_ledger(run_root_of[3], kwargs["plan"], sends=1)

        def mutate(document):
            document["new_provider_sends_started"] = 3

        _tamper_ledger(run_root_of[3], kwargs["plan"], mutate)
        _terminal_aux(run_root_of[3], 3)
        raise g7f.G7FExecutionError("terminal with inconsistent records")

    result = _execute(
        tmp_path,
        bundle_path=bundle_path,
        bundle=bundle,
        dpapi=FakeDpapi(),
        preflight=FakePreflight(),
        executor=FakeExecutor(handler=handler),
    )

    assert result["per_run_results"][0]["outcome"] == (
        g7r.RUN_OUTCOME_CORRUPT_STATE
    )
    assert result["newly_quarantined_set"] == [3]
    assert result["newly_terminal_set"] == []
    assert result["status"] == g7r.WAVE_STOPPED_AMBIGUOUS
    assert result["not_started_after_stop_set"] == [4]
    assert _tree_hash(run_root_of[4]) == untouched
    assert result["accounting_complete"] is False
    assert result["actual_new_provider_sends"] is None
    assert result["accounting_unavailable_run_indices"] == [3]


@pytest.mark.parametrize(
    "field,value", [("target_chapter", 4), ("run_index", 99)]
)
def test_ledger_with_wrong_run_or_chapter_binding_is_corrupt(
    tmp_path, monkeypatch, field, value
):
    root = _two_ready_runs(tmp_path)
    bundle_path, bundle = _bundle_for(tmp_path, monkeypatch, chapter=3, root=root)
    run_root_of = {
        item["run_index"]: Path(item["run_root"]) for item in bundle["runs"]
    }
    untouched = _tree_hash(run_root_of[4])

    def handler(self, kwargs):
        _canonical_ledger(run_root_of[3], kwargs["plan"], sends=1)

        def mutate(document):
            document[field] = value

        _tamper_ledger(run_root_of[3], kwargs["plan"], mutate)
        _terminal_aux(run_root_of[3], 3)
        raise g7f.G7FExecutionError("terminal with a wrong run/chapter binding")

    result = _execute(
        tmp_path,
        bundle_path=bundle_path,
        bundle=bundle,
        dpapi=FakeDpapi(),
        preflight=FakePreflight(),
        executor=FakeExecutor(handler=handler),
    )

    assert result["per_run_results"][0]["outcome"] == (
        g7r.RUN_OUTCOME_CORRUPT_STATE
    )
    assert result["newly_quarantined_set"] == [3]
    assert result["status"] == g7r.WAVE_STOPPED_AMBIGUOUS
    assert result["not_started_after_stop_set"] == [4]
    assert _tree_hash(run_root_of[4]) == untouched
    assert result["accounting_complete"] is False
    assert result["accounting_unavailable_run_indices"] == [3]


def test_completed_commit_with_a_tampered_ledger_is_not_accepted(
    tmp_path, monkeypatch
):
    root = _two_ready_runs(tmp_path)
    bundle_path, bundle = _bundle_for(tmp_path, monkeypatch, chapter=3, root=root)
    run_root_of = {
        item["run_index"]: Path(item["run_root"]) for item in bundle["runs"]
    }

    def handler(self, kwargs):
        _simulate_success(run_root_of[3], 3, kwargs["plan"])

        def mutate(document):
            document["accepted_credit_equivalent_usd"] = "0.5"

        _stale_hash_ledger(run_root_of[3], kwargs["plan"], mutate)
        return {"status": "COMPLETED"}

    result = _execute(
        tmp_path,
        bundle_path=bundle_path,
        bundle=bundle,
        dpapi=FakeDpapi(),
        preflight=FakePreflight(),
        executor=FakeExecutor(handler=handler),
    )

    # The chapter commit is visible on disk, but the tampered ledger means the
    # completed run is NOT accepted: fail closed, quarantine, stop the wave.
    assert (run_root_of[3] / "output" / "chapters" / "0003.md").is_file()
    assert result["completed_set"] == []
    assert result["per_run_results"][0]["outcome"] == (
        g7r.RUN_OUTCOME_CORRUPT_STATE
    )
    assert result["newly_quarantined_set"] == [3]
    assert result["status"] == g7r.WAVE_STOPPED_AMBIGUOUS
    assert result["fail_closed"] is True
    assert result["not_started_after_stop_set"] == [4]
    assert result["accounting_complete"] is False
    assert result["actual_new_provider_sends"] is None
    assert result["accounting_unavailable_run_indices"] == [3]


def test_completed_commit_with_ledger_bound_to_another_plan_is_not_accepted(
    tmp_path, monkeypatch
):
    root = _two_ready_runs(tmp_path)
    bundle_path, bundle = _bundle_for(tmp_path, monkeypatch, chapter=3, root=root)
    run_root_of = {
        item["run_index"]: Path(item["run_root"]) for item in bundle["runs"]
    }
    record_3 = next(item for item in bundle["runs"] if item["run_index"] == 3)
    plan_a = _plan_for(bundle_path, bundle, 3)
    # A second, genuinely distinct plan for the same run/chapter with different
    # ceilings: a real different plan, not a fabricated identity.
    plan_b = g7f.build_stage1_invocation_plan(
        stage1_root=bundle["stage1_root"],
        run_index=3,
        target_chapter=3,
        maximum_new_provider_sends=(
            record_3["maximum_new_provider_sends"] + 3
        ),
        maximum_credit_equivalent_usd=record_3[
            "maximum_credit_equivalent_usd"
        ],
    )
    assert plan_b["plan_sha256"] != plan_a["plan_sha256"]

    def handler(self, kwargs):
        if kwargs["plan"]["run_index"] == 3:
            _commit_chapter(run_root_of[3], 3)
            _canonical_ledger(run_root_of[3], plan_b, sends=1)
        else:
            _simulate_success(run_root_of[4], 3, kwargs["plan"])
        return {"status": "COMPLETED"}

    result = _execute(
        tmp_path,
        bundle_path=bundle_path,
        bundle=bundle,
        dpapi=FakeDpapi(),
        preflight=FakePreflight(),
        executor=FakeExecutor(handler=handler),
    )

    assert (run_root_of[3] / "output" / "chapters" / "0003.md").is_file()
    assert result["completed_set"] == []
    assert result["per_run_results"][0]["outcome"] == (
        g7r.RUN_OUTCOME_CORRUPT_STATE
    )
    assert result["newly_quarantined_set"] == [3]
    assert result["status"] == g7r.WAVE_STOPPED_AMBIGUOUS
    assert result["fail_closed"] is True
    assert result["not_started_after_stop_set"] == [4]
    assert result["accounting_complete"] is False
    assert result["accounting_unavailable_run_indices"] == [3]


def test_healthy_canonical_run_accounting_is_exact_and_unchanged(
    tmp_path, monkeypatch
):
    root = _two_ready_runs(tmp_path)
    bundle_path, bundle = _bundle_for(tmp_path, monkeypatch, chapter=3, root=root)
    run_root_of = {
        item["run_index"]: Path(item["run_root"]) for item in bundle["runs"]
    }
    created: dict[int, "g7f.InvocationBudgetLedger"] = {}

    def handler(self, kwargs):
        index = kwargs["plan"]["run_index"]
        _simulate_success(
            run_root_of[index], 3, kwargs["plan"], accepted_usage=(1200, 300)
        )
        created[index] = g7f.InvocationBudgetLedger.open_or_create(
            run_root=run_root_of[index],
            plan=kwargs["plan"],
            clock=lambda: NOW,
            allow_create=False,
        )
        return {"status": "COMPLETED"}

    result = _execute(
        tmp_path,
        bundle_path=bundle_path,
        bundle=bundle,
        dpapi=FakeDpapi(),
        preflight=FakePreflight(),
        executor=FakeExecutor(handler=handler),
    )

    expected_cost = created[3].accepted_cost + created[4].accepted_cost
    assert result["status"] == g7r.WAVE_COMPLETED
    assert result["completed_set"] == [3, 4]
    assert result["accounting_complete"] is True
    assert result["accounting_unavailable_run_indices"] == []
    assert result["actual_new_provider_sends"] == 2
    assert result["known_accounted_new_provider_sends"] == 2
    assert Decimal(result["accepted_credit_equivalent_usd"]) == expected_cost
    assert Decimal(result["known_accounted_credit_equivalent_usd"]) == (
        expected_cost
    )
    for line in result["actual_accounting_lines"]:
        assert line["status"] == g7r.ACCOUNTING_EXACT


def test_missing_ledger_after_response_recorded_quarantines(
    tmp_path, monkeypatch
):
    root = _two_ready_runs(tmp_path)
    bundle_path, bundle = _bundle_for(tmp_path, monkeypatch, chapter=3, root=root)
    run_root_of = {
        item["run_index"]: Path(item["run_root"]) for item in bundle["runs"]
    }
    untouched = _tree_hash(run_root_of[4])

    def handler(self, kwargs):
        _terminal_translation_journal(
            run_root_of[3],
            "0003.g7c_translation.json",
            chapter=3,
            spec=SPECS[3],
        )
        raise g7f.G7FExecutionError(
            "terminal translation without any target ledger"
        )

    result = _execute(
        tmp_path,
        bundle_path=bundle_path,
        bundle=bundle,
        dpapi=FakeDpapi(),
        preflight=FakePreflight(),
        executor=FakeExecutor(handler=handler),
    )

    assert result["per_run_results"][0]["outcome"] == (
        g7r.RUN_OUTCOME_CORRUPT_STATE
    )
    assert result["newly_terminal_set"] == []
    assert result["newly_quarantined_set"] == [3]
    assert result["status"] == g7r.WAVE_STOPPED_AMBIGUOUS
    assert result["not_started_after_stop_set"] == [4]
    assert _tree_hash(run_root_of[4]) == untouched
    assert result["accounting_complete"] is False
    assert result["actual_new_provider_sends"] is None
    assert result["accounting_unavailable_run_indices"] == [3]


def test_pre_send_local_failure_without_ledger_stays_exact_zero(
    tmp_path, monkeypatch
):
    root = _two_ready_runs(tmp_path)
    bundle_path, bundle = _bundle_for(tmp_path, monkeypatch, chapter=3, root=root)
    run_root_of = {
        item["run_index"]: Path(item["run_root"]) for item in bundle["runs"]
    }
    untouched = _tree_hash(run_root_of[4])

    def handler(self, kwargs):
        raise g7f.G7FExecutionError("local failure before any send")

    result = _execute(
        tmp_path,
        bundle_path=bundle_path,
        bundle=bundle,
        dpapi=FakeDpapi(),
        preflight=FakePreflight(),
        executor=FakeExecutor(handler=handler),
    )

    assert result["per_run_results"][0]["outcome"] == g7r.RUN_OUTCOME_LOCAL_FAILURE
    assert result["newly_quarantined_set"] == []
    assert result["status"] == g7r.WAVE_STOPPED_LOCAL_FAILURE
    assert not g7f.invocation_ledger_path(run_root_of[3], 3).exists()
    assert _tree_hash(run_root_of[4]) == untouched
    assert result["accounting_complete"] is True
    assert result["actual_new_provider_sends"] == 0
    assert Decimal(result["accepted_credit_equivalent_usd"]) == Decimal("0")
    assert result["accounting_unavailable_run_indices"] == []
    assert result["actual_accounting_lines"][0]["status"] == (
        g7r.ACCOUNTING_ZERO_PROVEN_PRE_SEND
    )


def test_g7r_validates_ledgers_via_the_frozen_invocation_budget_ledger(
    tmp_path, monkeypatch
):
    """G7R depends on the real frozen validator, not a two-field re-parse."""
    root = _two_ready_runs(tmp_path)
    bundle_path, bundle = _bundle_for(tmp_path, monkeypatch, chapter=3, root=root)
    run_root_of = {
        item["run_index"]: Path(item["run_root"]) for item in bundle["runs"]
    }
    plans = {index: _plan_for(bundle_path, bundle, index) for index in (3, 4)}
    for index in (3, 4):
        _canonical_ledger(run_root_of[index], plans[index], sends=1)

    calls: list[dict] = []
    real = g7f.InvocationBudgetLedger.open_or_create

    def spy(
        cls,
        *,
        run_root,
        plan,
        clock=None,
        allow_create=True,
    ):
        calls.append(
            {
                "run_root": Path(run_root),
                "plan_sha256": plan["plan_sha256"],
                "approval_token": plan["approval_token"],
                "allow_create": allow_create,
            }
        )
        return real(
            run_root=run_root,
            plan=plan,
            clock=clock,
            allow_create=allow_create,
        )

    monkeypatch.setattr(
        g7f.InvocationBudgetLedger, "open_or_create", classmethod(spy)
    )

    def handler(self, kwargs):
        _commit_chapter(run_root_of[kwargs["plan"]["run_index"]], 3)
        return {"status": "COMPLETED"}

    result = _execute(
        tmp_path,
        bundle_path=bundle_path,
        bundle=bundle,
        dpapi=FakeDpapi(),
        preflight=FakePreflight(),
        executor=FakeExecutor(handler=handler),
    )

    assert result["status"] == g7r.WAVE_COMPLETED
    validated = [call for call in calls if call["allow_create"] is False]
    assert validated
    assert {call["run_root"] for call in validated} == {
        run_root_of[3],
        run_root_of[4],
    }
    assert {call["plan_sha256"] for call in validated} == {
        plans[3]["plan_sha256"],
        plans[4]["plan_sha256"],
    }
    assert {call["approval_token"] for call in validated} == {
        plans[3]["approval_token"],
        plans[4]["approval_token"],
    }


# ---------------------------------------------------------------------------
# Q. G7S durable quarantine consumption
#
# A paid-boundary corrupt/unknown durable state can outlive the process that
# observed it, and the surviving artifacts of that boundary can look exactly
# like a healthy frontier.  G7S records it as one durable, create-once per-run
# marker; G7R must consume that marker strictly read-only, so a durably
# quarantined line can never be reconstructed as ready_for_target, is
# automatically excluded from derive_cohort/plan_wave, and is never executed.
# Marking is an explicit operator CLI act: no wave-time path writes a marker.
# ---------------------------------------------------------------------------


G7S_MARK_SENDS = 4
G7S_MARK_ACCEPTED_USAGE = (1000, 500)


def _mark_run(
    root,
    tmp_path,
    monkeypatch,
    *,
    run_index,
    first_blocked_chapter,
    controller_sha256=g7r.G7R_CONTROLLER_SHA256,
    control_name="control_mark",
):
    """Plan a real wave, build a real canonical ledger, then mark exactly once.

    The marker is created through the real G7S operator API against the real
    plan/ledger artifacts of this run, so it binds genuine durable evidence: the
    operator's exact wave-bundle provenance and the observing controller digest
    are both supplied explicitly.
    """
    _patch_run_binding(monkeypatch)
    head = "f" * 40
    control_dir = tmp_path / control_name
    bundle = g7r.plan_wave(
        stage1_root=root,
        target_chapter=first_blocked_chapter,
        control_dir=control_dir,
        repository_head=head,
        repository_head_resolver=lambda _root: head,
    )
    bundle_path = g7r.wave_bundle_path(control_dir, first_blocked_chapter)
    plan = _plan_for(bundle_path, bundle, run_index)
    run_root = Path(
        next(
            item for item in bundle["runs"] if item["run_index"] == run_index
        )["run_root"]
    )
    ledger = _canonical_ledger(
        run_root,
        plan,
        sends=G7S_MARK_SENDS,
        accepted_usage=G7S_MARK_ACCEPTED_USAGE,
    )
    accepted = ledger.document["accepted_credit_equivalent_usd"]
    result = g7s.mark_durable_quarantine(
        stage1_root=root,
        run_index=run_index,
        first_blocked_chapter=first_blocked_chapter,
        provider_sends_started=G7S_MARK_SENDS,
        accepted_credit_equivalent_usd=accepted,
        source_wave_bundle_sha256=bundle["bundle_sha256"],
        g7r_controller_sha256=controller_sha256,
        reason="paid boundary corrupt or unknown durable state",
    )
    return result["marker"]


def test_g7s_policy_is_bound_without_a_cycle():
    assert g7r.G7S_POLICY_SHA256 == g7s.G7S_POLICY_SHA256
    assert g7r.MARKER_STATE_VALID == g7s.MARKER_STATE_VALID == "valid"
    assert g7r.MARKER_STATE_INVALID == g7s.MARKER_STATE_INVALID == "invalid"

    policies = g7r._policy_bindings()
    key = "g7s_stage1_durable_quarantine_policy_sha256"
    assert policies[key] == g7s.G7S_POLICY_SHA256
    identity = g7r.g7r_controller_identity()
    assert identity["revision"] == (
        "goat-g7r-stage1-dynamic-wave-controller-v3"
    )
    assert identity["policies"][key] == g7s.G7S_POLICY_SHA256
    assert identity["provider_calls"] == 0


def test_every_run_disposition_carries_the_g7s_snapshot(tmp_path):
    root = _fixture_grid_root(tmp_path)

    report = g7r.build_status_report(stage1_root=root, target_chapter=3)

    assert report["durably_quarantined_run_indices"] == []
    assert report["active_run_indices"] == [3, 4, 5, 7, 8, 9, 10]
    for run in report["runs"]:
        assert run["durable_quarantine"]["state"] == (
            g7s.MARKER_STATE_ABSENT
        )
        assert run["durable_quarantine"]["reason"] is None
        assert "durable_quarantine" in run

    # Terminal, quarantined and corrupt lines all keep their (absent) snapshot.
    for index, expected in (
        (1, g7r.DISPOSITION_TERMINAL),
        (6, g7r.DISPOSITION_QUARANTINED),
        (7, g7r.DISPOSITION_READY_FOR_TARGET),
    ):
        disposition = g7r.derive_run_disposition(root, SPECS[index], 3)
        assert disposition["disposition"] == expected
        assert disposition["durable_quarantine"]["state"] == (
            g7s.MARKER_STATE_ABSENT
        )

    missing = g7r.derive_run_disposition(tmp_path / "nowhere", SPECS[1], 3)
    assert missing["disposition"] == g7r.DISPOSITION_CORRUPT
    assert missing["durable_quarantine"]["state"] == g7s.MARKER_STATE_ABSENT


def test_invalid_durable_marker_dominates_and_fails_closed(
    tmp_path, monkeypatch
):
    root = _fixture_grid_root(tmp_path)
    spec = SPECS[3]
    run_root = root / spec["relative_root"]
    marker_path = g7s.durable_quarantine_marker_path(run_root)
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text("{not json", encoding="utf-8")
    # An ambiguous journal is present as well: the durable G7S refutation still
    # dominates every other cause and the run is corrupt, never merely
    # quarantined.
    _uncertain_journal(
        run_root,
        "0003.g7c_translation.json",
        operation_kind="translation",
        chapter=3,
        spec=spec,
    )

    disposition = g7r.derive_run_disposition(root, spec, 3)

    assert disposition["disposition"] == g7r.DISPOSITION_CORRUPT
    assert disposition["disposition"] != g7r.DISPOSITION_QUARANTINED
    assert "G7S durable marker" in disposition["detail"]
    assert "marker_unreadable" in disposition["detail"]
    assert disposition["progress"] is None
    assert disposition["evidence"] is None
    assert disposition["durable_quarantine"]["state"] == (
        g7s.MARKER_STATE_INVALID
    )

    report = g7r.build_status_report(stage1_root=root, target_chapter=3)
    assert 3 in report["corrupt_run_indices"]
    assert 3 not in report["active_run_indices"]
    assert 3 not in report["quarantined_run_indices"]
    assert report["durably_quarantined_run_indices"] == []


def test_valid_durable_marker_blocks_its_chapter_and_every_later_one(
    tmp_path, monkeypatch
):
    root = _fixture_grid_root(tmp_path)
    spec = SPECS[3]
    blocked = 5
    # A realistic dedicated fixture: Run 03 is a healthy line whose frontier is
    # chapter 5, so a real chapter-5 wave plans it and a real canonical
    # chapter-5 ledger exists for the blocked chapter.  The marker can therefore
    # be created only through the real G7S operator API against genuine paid
    # boundary evidence.
    run_root = _write_run(root, 3, progress_last=4)
    _healthy_pair(run_root, spec)

    marker = _mark_run(
        root, tmp_path, monkeypatch, run_index=3, first_blocked_chapter=blocked
    )
    assert marker["first_blocked_chapter"] == blocked
    assert marker["provider_sends_started"] == G7S_MARK_SENDS

    # Below the blocked chapter the historical classification is untouched.
    below = g7r.derive_run_disposition(root, spec, 4)
    assert below["disposition"] == g7r.DISPOSITION_HEALTHY_COMPLETED
    assert below["disposition"] != g7r.DISPOSITION_QUARANTINED
    assert below["durable_quarantine"]["state"] == g7s.MARKER_STATE_VALID

    for chapter in (blocked, blocked + 1):
        disposition = g7r.derive_run_disposition(root, spec, chapter)
        assert disposition["disposition"] == g7r.DISPOSITION_QUARANTINED
        assert "durable G7S quarantine marker" in disposition["detail"]
        assert f"first_blocked_chapter={blocked}" in disposition["detail"]
        assert g7s.DURABLE_OUTCOME_CORRUPT_OR_UNKNOWN_STATE in (
            disposition["detail"]
        )

    status_below = g7r.build_status_report(stage1_root=root, target_chapter=4)
    assert status_below["durably_quarantined_run_indices"] == []
    assert 3 not in status_below["quarantined_run_indices"]

    status_at = g7r.build_status_report(
        stage1_root=root, target_chapter=blocked
    )
    assert status_at["durably_quarantined_run_indices"] == [3]
    assert 3 in status_at["quarantined_run_indices"]
    assert 3 not in status_at["active_run_indices"]


def test_historical_controller_digest_stays_valid_and_drift_is_comparison_only(
    tmp_path, monkeypatch
):
    """Requirement 8: the stored controller digest is historical provenance.

    A marker written by an earlier observing G7R controller stays VALID and keeps
    blocking its chapter and every later one; controller drift is reported only
    when an operator supplies an explicit expected digest.
    """
    root = _fixture_grid_root(tmp_path)
    run_root = root / SPECS[3]["relative_root"]
    historical = "a" * 64
    assert historical != g7r.G7R_CONTROLLER_SHA256

    marker = _mark_run(
        root,
        tmp_path,
        monkeypatch,
        run_index=3,
        first_blocked_chapter=3,
        controller_sha256=historical,
    )

    assert marker["g7r_controller_sha256"] == historical

    # No expectation supplied: the historical digest is provenance only.
    snapshot = g7s.inspect_durable_quarantine_marker(run_root)
    assert snapshot["state"] == g7s.MARKER_STATE_VALID

    disposition = g7r.derive_run_disposition(root, SPECS[3], 3)
    assert disposition["disposition"] == g7r.DISPOSITION_QUARANTINED
    assert disposition["durable_quarantine"]["state"] == g7s.MARKER_STATE_VALID

    report = g7r.build_status_report(stage1_root=root, target_chapter=3)
    assert report["durably_quarantined_run_indices"] == [3]
    assert 3 not in report["active_run_indices"]

    matching = g7s.inspect_durable_quarantine_marker(
        run_root, expected_g7r_controller_sha256=historical
    )
    assert matching["state"] == g7s.MARKER_STATE_VALID

    drifted = g7s.inspect_durable_quarantine_marker(
        run_root,
        expected_g7r_controller_sha256=g7r.G7R_CONTROLLER_SHA256,
    )
    assert drifted["state"] == g7s.MARKER_STATE_INVALID
    assert drifted["reason"] == "g7r_controller_drift"


def test_status_report_exposes_the_durable_quarantine_evidence(
    tmp_path, monkeypatch
):
    root = _fixture_grid_root(tmp_path)
    _mark_run(
        root, tmp_path, monkeypatch, run_index=3, first_blocked_chapter=3
    )

    report = g7r.build_status_report(stage1_root=root, target_chapter=3)

    assert report["durably_quarantined_run_indices"] == [3]
    assert report["quarantined_run_indices"] == [3, 6, 12]
    assert report["active_run_indices"] == [4, 5, 7, 8, 9, 10]
    assert report["disposition_counts"] == {
        "quarantined_ambiguous_transport": 3,
        "ready_for_target": 6,
        "terminal_durable_model_or_policy_failure": 3,
    }
    by_index = {run["run_index"]: run for run in report["runs"]}
    snapshot = by_index[3]["durable_quarantine"]
    assert snapshot["state"] == g7s.MARKER_STATE_VALID
    assert snapshot["first_blocked_chapter"] == 3
    assert snapshot["source_outcome"] == (
        g7s.DURABLE_OUTCOME_CORRUPT_OR_UNKNOWN_STATE
    )
    assert snapshot["provider_sends_started"] == G7S_MARK_SENDS
    assert snapshot["reason"] is None
    assert by_index[3]["disposition"] == g7r.DISPOSITION_QUARANTINED
    assert by_index[3]["detail"].startswith("durable G7S quarantine marker")
    assert by_index[3]["terminal_evidence"] == []
    for index in (4, 6, 12):
        assert by_index[index]["durable_quarantine"]["state"] == (
            g7s.MARKER_STATE_ABSENT
        )
    assert report["zero_provider_verification"]["provider_calls"] == 0
    assert report["zero_provider_verification"]["credential_access"] is False


def test_wave_bundle_records_durable_quarantine_additively(
    tmp_path, monkeypatch
):
    root = _fixture_grid_root(tmp_path)
    _mark_run(
        root, tmp_path, monkeypatch, run_index=3, first_blocked_chapter=3
    )

    bundle_path, bundle = _bundle_for(
        tmp_path, monkeypatch, chapter=3, root=root
    )

    assert bundle["status"] == g7r.BUNDLE_STATUS_READY
    assert bundle["durably_quarantined_run_indices"] == [3]
    assert bundle["active_run_indices"] == [4, 5, 7, 8, 9, 10]
    dispositions = {
        item["run_index"]: item for item in bundle["cohort_dispositions"]
    }
    assert sorted(dispositions) == list(range(1, 13))
    for item in bundle["cohort_dispositions"]:
        assert "durable_quarantine" in item
    assert dispositions[3]["disposition"] == g7r.DISPOSITION_QUARANTINED
    assert dispositions[3]["durable_quarantine"]["state"] == (
        g7s.MARKER_STATE_VALID
    )
    assert dispositions[6]["durable_quarantine"]["state"] == (
        g7s.MARKER_STATE_ABSENT
    )
    assert bundle["zero_provider_verification"]["provider_calls"] == 0

    plans = sorted(
        path.name for path in (bundle_path.parent / "plans").iterdir()
    )
    assert len(plans) == len(bundle["runs"]) == 6
    assert not any(name.startswith("run_03_") for name in plans)


def test_stale_g7s_policy_binding_fails_closed(tmp_path, monkeypatch):
    root = _all_ready_root(tmp_path)
    _, bundle = _bundle_for(tmp_path, monkeypatch, chapter=3, root=root)
    key = "g7s_stage1_durable_quarantine_policy_sha256"
    assert bundle["policies"][key] == g7s.G7S_POLICY_SHA256

    stale = copy.deepcopy(bundle)
    stale["policies"][key] = "0" * 64
    stale["bundle_sha256"] = g7r.compute_bundle_sha256(stale)

    with pytest.raises(g7r.G7RBundleError, match="stale repository policy"):
        g7r.verify_wave_bundle(
            stale, expected_bundle_sha256=stale["bundle_sha256"]
        )


def test_marked_line_is_never_ready_and_its_evidence_is_untouched(
    tmp_path, monkeypatch
):
    root = _fixture_grid_root(tmp_path)
    spec = SPECS[3]
    run_root = root / spec["relative_root"]
    # A durable chapter-3 target response: the surviving artifact of the paid
    # boundary that a marker-free reconstruction would read as a healthy
    # frontier.
    response = _response_recorded_response(
        "translated prose",
        "stop",
        diagnostic={
            "status": "ok",
            "visible_content_empty": False,
            "finish_reason": "stop",
            "completion_tokens": 100,
            "max_completion_tokens": 24000,
            "near_completion_ceiling": False,
        },
    )
    journal_path = (
        run_root / "data" / "provider_request_journal" / "0003.g7c_translation.json"
    )
    _write_journal(
        run_root,
        journal_path.name,
        _journal_record(
            status="response_recorded",
            operation_kind="translation",
            chapter=3,
            model=spec["model_id"],
            novel_id=spec["novel_id"],
            response=response,
            response_sha256=_canon_sha(response),
        ),
    )

    before = g7r.derive_run_disposition(root, spec, 3)
    assert before["disposition"] == g7r.DISPOSITION_READY_FOR_TARGET
    assert before["durable_quarantine"]["state"] == g7s.MARKER_STATE_ABSENT

    marker = _mark_run(
        root, tmp_path, monkeypatch, run_index=3, first_blocked_chapter=3
    )
    assert marker["first_blocked_chapter"] == 3
    assert marker["provider_sends_started"] == G7S_MARK_SENDS
    ledger_path = g7f.invocation_ledger_path(run_root, 3)
    ledger_document = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert ledger_document["new_provider_sends_started"] == G7S_MARK_SENDS
    assert Decimal(marker["accepted_credit_equivalent_usd"]) == Decimal(
        ledger_document["accepted_credit_equivalent_usd"]
    )

    watched = {
        "progress": run_root / "data" / "progress.json",
        "journal": journal_path,
        "ledger": ledger_path,
    }
    before_bytes = {name: path.read_bytes() for name, path in watched.items()}

    after = g7r.derive_run_disposition(root, spec, 3)
    assert after["disposition"] == g7r.DISPOSITION_QUARANTINED
    assert after["disposition"] != g7r.DISPOSITION_READY_FOR_TARGET
    assert "durable G7S quarantine marker" in after["detail"]

    report = g7r.build_status_report(stage1_root=root, target_chapter=3)
    assert 3 in report["quarantined_run_indices"]
    assert report["durably_quarantined_run_indices"] == [3]

    # The G7S check is read-only: it changed no journal, ledger, or progress
    # byte, and only added the operator's own marker.
    assert {
        name: path.read_bytes() for name, path in watched.items()
    } == before_bytes
    assert (run_root / "data" / "g7s_durable_quarantine.json").is_file()


def test_plan_excludes_a_marked_run_and_materializes_no_plan_for_it(
    tmp_path, monkeypatch
):
    root = _fixture_grid_root(tmp_path)
    _mark_run(
        root, tmp_path, monkeypatch, run_index=3, first_blocked_chapter=3
    )

    bundle_path, bundle = _bundle_for(
        tmp_path, monkeypatch, chapter=3, root=root
    )

    assert bundle["active_run_indices"] == [4, 5, 7, 8, 9, 10]
    assert bundle["active_count"] == 6
    assert [record["run_index"] for record in bundle["runs"]] == [
        4,
        5,
        7,
        8,
        9,
        10,
    ]
    assert bundle["durably_quarantined_run_indices"] == [3]
    assert 3 not in bundle["active_run_indices"]
    forbidden = g7r._plan_filename(3, SPECS[3]["relative_root"])
    assert not (bundle_path.parent / "plans" / forbidden).exists()
    assert bundle_path.is_file()


def test_marking_never_executes_the_next_chapter(tmp_path, monkeypatch):
    root = _fixture_grid_root(tmp_path)

    def handler(self, kwargs):
        raise AssertionError("no run may execute during marking or planning")

    executor = FakeExecutor(handler=handler)

    _mark_run(
        root, tmp_path, monkeypatch, run_index=3, first_blocked_chapter=3
    )
    progress_before = {
        index: (
            root / SPECS[index]["relative_root"] / "data" / "progress.json"
        ).read_bytes()
        for index in range(1, 13)
    }

    _, bundle = _bundle_for(tmp_path, monkeypatch, chapter=3, root=root)

    assert bundle["target_chapter"] == 3
    assert bundle["active_run_indices"] == [4, 5, 7, 8, 9, 10]
    assert not list(tmp_path.glob("**/wave_chapter_04"))
    assert not list(tmp_path.glob("**/wave_chapter_05"))
    assert executor.calls == []
    assert not list(root.glob("*/output/chapters/0004.md"))
    assert {
        index: (
            root / SPECS[index]["relative_root"] / "data" / "progress.json"
        ).read_bytes()
        for index in range(1, 13)
    } == progress_before


def test_a_marked_line_cannot_execute_a_stale_bundle(tmp_path, monkeypatch):
    root = _fixture_grid_root(tmp_path)
    bundle_path, bundle = _bundle_for(
        tmp_path, monkeypatch, chapter=3, root=root
    )
    assert 3 in bundle["active_run_indices"]

    _mark_run(
        root, tmp_path, monkeypatch, run_index=3, first_blocked_chapter=3
    )

    dpapi = FakeDpapi()
    executor = FakeExecutor(handler=lambda self, kwargs: {"status": "COMPLETED"})

    with pytest.raises(g7r.G7RBundleError, match="no longer ready"):
        _execute(
            tmp_path,
            bundle_path=bundle_path,
            bundle=bundle,
            dpapi=dpapi,
            preflight=FakePreflight(),
            executor=executor,
        )

    assert executor.calls == []
    assert dpapi.events == []
    assert dpapi.unprotect_calls == []
