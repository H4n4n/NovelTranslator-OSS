"""Focused tests for the G7H zero-provider Stage-1 readiness doctor.

Every test makes zero real Provider calls. Provider access, network access, and
real DPAPI access are all replaced by fakes or forbidden outright.
"""

import builtins
import hashlib
import importlib
import json
import os
import socket
from pathlib import Path

import pytest

import benchmark_longitudinal_g7f_stage1_paid_executor as g7f
import benchmark_longitudinal_g7g_stage1_secure_launcher as g7g
import benchmark_longitudinal_g7h_readiness_doctor as g7h
import benchmark_longitudinal_provider as provider
from benchmark_longitudinal_g7c_stage1_integration import stage1_run_specs


SECRET_KEY = "sk-g7h-test-secret-0123456789abcdef"
LEGACY_KEY = "sk-g7h-legacy-secret-fedcba9876543210"
PROTECTED_PREFIX = b"g7h-protected:"

REPO_ROOT = Path(__file__).resolve().parents[1]
G7G_FILENAME = "benchmark_longitudinal_g7g_stage1_secure_launcher.py"
G7F_FILENAME = "benchmark_longitudinal_g7f_stage1_paid_executor.py"


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical(value) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha_json(value) -> str:
    return _sha256_bytes(_canonical(value).encode("utf-8"))


class FakeDpapi:
    """Deterministic stand-in for the G7G Windows DPAPI backend."""

    def __init__(self, *, fail_unprotect: bool = False):
        self.fail_unprotect = fail_unprotect
        self.protect_calls: list[bytes] = []
        self.unprotect_calls: list[bytes] = []

    def protect(self, plaintext: bytes) -> bytes:
        self.protect_calls.append(bytes(plaintext))
        return PROTECTED_PREFIX + bytes(plaintext)[::-1]

    def unprotect(self, ciphertext: bytes) -> bytes:
        self.unprotect_calls.append(bytes(ciphertext))
        if self.fail_unprotect:
            raise ValueError("corrupted protected secret")
        if not bytes(ciphertext).startswith(PROTECTED_PREFIX):
            raise ValueError("not a protected envelope")
        return bytes(ciphertext)[len(PROTECTED_PREFIX):][::-1]


class FakeExecutor:
    """Zero-provider stand-in for the real G7F authorization check."""

    def __init__(self, *, status=None, overrides=None, drop=()):
        self.status = status or g7g.AUTHORIZATION_REQUIRED_STATUS
        self.overrides = dict(overrides or {})
        self.drop = tuple(drop)
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        plan = kwargs["plan"]
        result = {
            "status": self.status,
            "paid_execution_authorized": False,
            "plan_sha256": plan.get("plan_sha256"),
            "approval_token": plan.get("approval_token"),
            "run_index": plan.get("run_index"),
            "target_chapter": plan.get("target_chapter"),
            "provider_calls": 0,
        }
        result.update(self.overrides)
        for field in self.drop:
            result.pop(field, None)
        return result


def _g7g_preflight_runner(executor):
    def run(**kwargs):
        return g7g.run_preflight(**kwargs, executor=executor)

    return run


def _raw_preflight_runner(plan, *, overrides=None, drop=()):
    def run(**_kwargs):
        result = {
            "status": "PREFLIGHT_OK",
            "authorization_required": True,
            "paid_execution_authorized": False,
            "provider_calls": 0,
            "run_index": plan["run_index"],
            "target_chapter": plan["target_chapter"],
            "plan_sha256": plan["plan_sha256"],
            "approval_token": plan["approval_token"],
        }
        result.update(dict(overrides or {}))
        for field in drop:
            result.pop(field, None)
        return result

    return run


def _plan(*, run_index=1, target=1, plan_sha=None, approval_token=None):
    spec = stage1_run_specs()[0]
    plan = {
        "schema_version": g7f.G7F_SCHEMA_VERSION,
        "revision": g7f.G7F_REVISION,
        "g7f_executor_sha256": g7f.G7F_EXECUTOR_SHA256,
        "stage1_roots_manifest_sha256": (
            g7f.EXPECTED_STAGE1_ROOTS_MANIFEST_SHA256
        ),
        "run_index": run_index,
        "relative_root": spec["relative_root"],
        "target_chapter": target,
        "approval_token": approval_token or ("a" * 64),
    }
    plan["plan_sha256"] = plan_sha or _sha_json(plan)
    return plan


def _journal_record(status):
    record = {
        "schema_version": 1,
        "revision": "paid-request-journal-v1",
        "operation_kind": "translation",
        "novel_id": "fixture",
        "chapter": 1,
        "provider": "command_code",
        "model": "fixture/model",
        "profile_id": "fixture",
        "source_sha256": "1" * 64,
        "translation_sha256": "",
        "request_sha256": "2" * 64,
        "operation_id": "3" * 64,
        "status": status,
        "attempt_context": {},
        "response": {},
        "response_sha256": "",
        "error": {},
        "created_utc": "2026-09-21T00:00:00+00:00",
        "updated_utc": "2026-09-21T00:00:00+00:00",
    }
    record["record_sha256"] = _sha_json(record)
    return record


def _valid_ledger_document():
    spec = stage1_run_specs()[0]
    document = {
        "schema_version": g7f.LEDGER_SCHEMA_VERSION,
        "revision": g7f.LEDGER_REVISION,
        "plan_sha256": "a" * 64,
        "approval_token": "b" * 64,
        "run_index": 1,
        "relative_root": spec["relative_root"],
        "model_id": spec["model_id"],
        "novel_id": spec["novel_id"],
        "target_chapter": 1,
        "maximum_new_provider_sends": 1,
        "maximum_credit_equivalent_usd": "1",
        "new_provider_sends_started": 0,
        "accepted_credit_equivalent_usd": "0",
        "records": [],
        "created_utc": "2026-09-21T00:00:00+00:00",
        "updated_utc": "2026-09-21T00:00:00+00:00",
    }
    document["ledger_sha256"] = g7f._ledger_hash(document)
    return document


def _build_stage1(
    tmp_path,
    *,
    dirname="stage1",
    chapter_output=False,
    journal_payload=None,
    ledger_payload=None,
):
    root = tmp_path / dirname
    spec = stage1_run_specs()[0]
    run_root = root / Path(spec["relative_root"])

    (run_root / "data" / "provider_request_journal").mkdir(parents=True)
    (run_root / "data" / "glossary_candidate_resolutions").mkdir(parents=True)
    (run_root / "output" / "chapters").mkdir(parents=True)
    (run_root / "output" / "pending").mkdir(parents=True)
    (run_root / "output" / "qa").mkdir(parents=True)
    (run_root / "output" / "audits").mkdir(parents=True)
    (run_root / "source").mkdir(parents=True)
    (root / "g7c_stage1_roots_manifest.json").write_text(
        "{}\n", encoding="utf-8"
    )
    (run_root / "novel.json").write_text("{}\n", encoding="utf-8")
    (run_root / "data" / "chapters.json").write_text("[]\n", encoding="utf-8")
    (run_root / "data" / "progress.json").write_text(
        json.dumps(
            {"completed": [], "last_completed": 0, "next_chapter": 1}
        ),
        encoding="utf-8",
    )

    if chapter_output:
        (run_root / "output" / "chapters" / "0001.md").write_text(
            "chapter one\n", encoding="utf-8"
        )
    if journal_payload is not None:
        payload = (
            journal_payload
            if isinstance(journal_payload, str)
            else json.dumps(journal_payload)
        )
        (
            run_root
            / "data"
            / "provider_request_journal"
            / "0001.translation.json"
        ).write_text(payload, encoding="utf-8")
    if ledger_payload is not None:
        ledger_dir = run_root / Path(g7f.LEDGER_DIRECTORY)
        ledger_dir.mkdir(parents=True)
        payload = (
            ledger_payload
            if isinstance(ledger_payload, str)
            else json.dumps(ledger_payload)
        )
        (ledger_dir / "0001.json").write_text(payload, encoding="utf-8")

    return root, run_root


def _write_active_secret(tmp_path, dpapi, key=SECRET_KEY):
    path = tmp_path / "command_code_api_key.dpapi"
    path.write_bytes(dpapi.protect(key.encode("utf-8")))
    return path


def _write_legacy_backup(tmp_path, key=LEGACY_KEY):
    path = tmp_path / "command_code_api_key.legacy-backup.dpapi"
    path.write_bytes(b"legacy-envelope:" + key.encode("utf-8"))
    return path


def _prepare(
    tmp_path,
    *,
    plan=None,
    dpapi=None,
    preflight_runner=None,
    stage1_kwargs=None,
    **overrides,
):
    dpapi = dpapi if dpapi is not None else FakeDpapi()
    plan = plan if plan is not None else _plan()
    root, run_root = _build_stage1(tmp_path, **(stage1_kwargs or {}))
    secret = _write_active_secret(tmp_path, dpapi)
    legacy = _write_legacy_backup(tmp_path)
    identity = g7h.stage1_tree_fingerprint(root)
    plan_path = tmp_path / "g7f_invocation_plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")

    kwargs = {
        "code_root": REPO_ROOT,
        "stage1_root": root,
        "plan_path": plan_path,
        "expected_plan_sha256": plan["plan_sha256"],
        "expected_approval_token": plan["approval_token"],
        "secret_path": secret,
        "expected_secret_sha256": _sha256_bytes(secret.read_bytes()),
        "expected_secret_bytes": secret.stat().st_size,
        "legacy_backup": legacy,
        "expected_legacy_sha256": _sha256_bytes(legacy.read_bytes()),
        "expected_legacy_bytes": legacy.stat().st_size,
        "expected_stage1_files": identity["file_count"],
        "expected_stage1_fingerprint": identity["fingerprint"],
        "expected_g7g_sha256": _sha256_bytes(
            (REPO_ROOT / G7G_FILENAME).read_bytes()
        ),
        "expected_g7f_sha256": _sha256_bytes(
            (REPO_ROOT / G7F_FILENAME).read_bytes()
        ),
        "preflight_runner": (
            preflight_runner
            if preflight_runner is not None
            else _g7g_preflight_runner(FakeExecutor())
        ),
        "dpapi": dpapi,
        "platform_resolver": lambda: True,
    }
    kwargs.update(overrides)
    context = {
        "root": root,
        "run_root": run_root,
        "secret": secret,
        "legacy": legacy,
        "plan": plan,
        "dpapi": dpapi,
        "plan_path": plan_path,
        "identity": identity,
    }
    return kwargs, context


# ---------------------------------------------------------------------------
# import safety
# ---------------------------------------------------------------------------


def test_import_has_no_side_effects(monkeypatch):
    opened: list[tuple[str, str]] = []
    real_open = builtins.open

    def guarded_open(file, mode="r", *args, **kwargs):
        opened.append((str(file), mode))
        if any(flag in mode for flag in ("w", "a", "x", "+")):
            raise AssertionError(f"import-time write: {file}")
        return real_open(file, mode, *args, **kwargs)

    def forbidden_socket(*_args, **_kwargs):
        raise AssertionError("import-time network access")

    def forbidden_replace(*_args, **_kwargs):
        raise AssertionError("import-time filesystem replace")

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(socket, "socket", forbidden_socket)
    monkeypatch.setattr(socket, "create_connection", forbidden_socket)
    monkeypatch.setattr(os, "replace", forbidden_replace)

    importlib.reload(g7h)

    assert g7h.G7H_REVISION == "goat-g7h-zero-provider-readiness-doctor-v1"
    lowered = [path.lower() for path, _mode in opened]
    assert not any(".env" in path for path in lowered)
    assert not any("secret" in path or "dpapi" in path for path in lowered)


def test_module_never_authorizes_or_executes():
    source = Path(g7h.__file__).read_text(encoding="utf-8")
    for token in (
        "operator_authorized=True",
        "authorize_paid_run",
        "create_provider_client",
        "send_chat_completion_once",
        "subprocess",
        "dotenv",
        "load_dotenv",
        "getenv",
        "openai",
        "cmdc",
        ".env",
    ):
        assert token not in source, token


def test_module_has_executable_main_guard():
    source = Path(g7h.__file__).read_text(encoding="utf-8")
    assert 'if __name__ == "__main__":' in source
    assert "raise SystemExit(main())" in source


# ---------------------------------------------------------------------------
# exact successful readiness path
# ---------------------------------------------------------------------------


def test_successful_readiness_path(tmp_path):
    kwargs, context = _prepare(tmp_path)

    report = g7h.run_readiness(**kwargs)

    plan = context["plan"]
    secret = context["secret"]
    legacy = context["legacy"]

    assert report["status"] == "READINESS_OK"
    assert report["provider_calls"] == 0
    assert report["paid_execution_authorized"] is False
    assert report["chapter1_executed"] is False
    assert report["authorization_required"] is True
    assert report["run_index"] == 1
    assert report["target_chapter"] == 1
    assert report["plan_sha256"] == plan["plan_sha256"]
    assert report["approval_token"] == plan["approval_token"]

    assert report["preflight"]["status"] == "PREFLIGHT_OK"
    assert report["preflight"]["provider_calls"] == 0
    assert report["preflight"]["paid_execution_authorized"] is False

    assert (
        report["stage1"]["file_count"]
        == context["identity"]["file_count"]
    )
    assert (
        report["stage1"]["fingerprint"]
        == context["identity"]["fingerprint"]
    )
    assert report["stage1"]["chapter_pending_outputs"] == 0

    assert report["active_secret"]["readable"] is True
    assert (
        report["active_secret"]["encrypted_bytes"]
        == secret.stat().st_size
    )
    assert report["active_secret"]["sha256"] == _sha256_bytes(
        secret.read_bytes()
    )

    assert report["legacy_backup"]["decrypted"] is False
    assert (
        report["legacy_backup"]["encrypted_bytes"]
        == legacy.stat().st_size
    )
    assert report["legacy_backup"]["sha256"] == _sha256_bytes(
        legacy.read_bytes()
    )

    assert report["paid_artifacts"]["journal"] == "ABSENT"
    assert report["paid_artifacts"]["ledger"] == "ABSENT"

    assert SECRET_KEY not in json.dumps(report)
    assert LEGACY_KEY not in json.dumps(report)


def test_required_json_field_shapes_are_present(tmp_path):
    kwargs, _context = _prepare(tmp_path)

    report = g7h.run_readiness(**kwargs)

    assert set(
        ["status", "provider_calls", "paid_execution_authorized",
         "chapter1_executed", "run_index", "target_chapter",
         "plan_sha256", "approval_token", "preflight", "stage1",
         "active_secret", "legacy_backup", "paid_artifacts"]
    ) <= set(report)
    assert {"status", "provider_calls", "paid_execution_authorized"} <= set(
        report["preflight"]
    )
    assert {"file_count", "fingerprint", "chapter_pending_outputs"} <= set(
        report["stage1"]
    )
    assert {"readable", "encrypted_bytes", "sha256"} <= set(
        report["active_secret"]
    )
    assert {"decrypted", "encrypted_bytes", "sha256"} <= set(
        report["legacy_backup"]
    )
    assert {"journal", "ledger"} <= set(report["paid_artifacts"])


# ---------------------------------------------------------------------------
# identity drift
# ---------------------------------------------------------------------------


def test_wrong_g7g_identity_fails(tmp_path):
    kwargs, _context = _prepare(tmp_path, expected_g7g_sha256="0" * 64)

    with pytest.raises(g7h.G7HReadinessError, match="G7G"):
        g7h.run_readiness(**kwargs)


def test_wrong_g7f_identity_fails(tmp_path):
    kwargs, _context = _prepare(tmp_path, expected_g7f_sha256="0" * 64)

    with pytest.raises(g7h.G7HReadinessError, match="G7F"):
        g7h.run_readiness(**kwargs)


def test_missing_g7g_source_fails(tmp_path):
    code_root = tmp_path / "code"
    code_root.mkdir()
    kwargs, _context = _prepare(tmp_path, code_root=code_root)

    with pytest.raises(g7h.G7HReadinessError, match="G7G launcher source missing"):
        g7h.run_readiness(**kwargs)


def test_wrong_plan_sha_fails(tmp_path):
    kwargs, _context = _prepare(tmp_path, expected_plan_sha256="f" * 64)

    with pytest.raises(g7h.G7HReadinessError, match="plan SHA-256"):
        g7h.run_readiness(**kwargs)


def test_wrong_approval_token_fails(tmp_path):
    kwargs, _context = _prepare(tmp_path, expected_approval_token="e" * 64)

    with pytest.raises(g7h.G7HReadinessError, match="approval token"):
        g7h.run_readiness(**kwargs)


def test_wrong_run_index_fails(tmp_path):
    plan = _plan(run_index=2)
    kwargs, _context = _prepare(tmp_path, plan=plan)

    with pytest.raises(g7h.G7HReadinessError, match="run index"):
        g7h.run_readiness(**kwargs)


def test_wrong_target_chapter_fails(tmp_path):
    plan = _plan(target=2)
    kwargs, _context = _prepare(tmp_path, plan=plan)

    with pytest.raises(g7h.G7HReadinessError, match="target chapter"):
        g7h.run_readiness(**kwargs)


def test_wrong_relative_root_fails(tmp_path):
    kwargs, context = _prepare(tmp_path)
    plan = dict(context["plan"])
    plan["relative_root"] = "runs/other"
    plan.pop("plan_sha256")
    plan["plan_sha256"] = _sha_json(plan)
    Path(context["plan_path"]).write_text(json.dumps(plan), encoding="utf-8")
    kwargs["expected_plan_sha256"] = plan["plan_sha256"]

    with pytest.raises(g7h.G7HReadinessError, match="relative root"):
        g7h.run_readiness(**kwargs)


def test_plan_binding_another_g7f_executor_fails(tmp_path):
    kwargs, context = _prepare(tmp_path)
    plan = dict(context["plan"])
    plan["g7f_executor_sha256"] = "9" * 64
    plan.pop("plan_sha256")
    plan["plan_sha256"] = _sha_json(plan)
    Path(context["plan_path"]).write_text(json.dumps(plan), encoding="utf-8")
    kwargs["expected_plan_sha256"] = plan["plan_sha256"]

    with pytest.raises(g7h.G7HReadinessError, match="G7F executor identity"):
        g7h.run_readiness(**kwargs)


# ---------------------------------------------------------------------------
# Stage-1 contract
# ---------------------------------------------------------------------------


def test_stage1_file_count_drift_fails(tmp_path):
    kwargs, context = _prepare(tmp_path)
    kwargs["expected_stage1_files"] = context["identity"]["file_count"] + 1

    with pytest.raises(g7h.G7HReadinessError, match="file count drifted"):
        g7h.run_readiness(**kwargs)


def test_stage1_fingerprint_drift_fails(tmp_path):
    kwargs, _context = _prepare(tmp_path, expected_stage1_fingerprint="0" * 64)

    with pytest.raises(g7h.G7HReadinessError, match="fingerprint drifted"):
        g7h.run_readiness(**kwargs)


def _frozen_evidence_rows(root):
    """Independent reimplementation of the frozen evidence-tree row convention.

    Rows are ``relative-posix-path<TAB>size<TAB>sha256`` ordered by resolved
    path and joined with LF, exactly as the frozen external harness produced
    the certified Stage-1 tree identity.
    """
    root = Path(root).resolve()
    rows = []
    for item in sorted(
        (path for path in root.rglob("*") if path.is_file()),
        key=lambda path: str(path.resolve()),
    ):
        resolved = item.resolve()
        rows.append(
            "%s\t%d\t%s"
            % (
                resolved.relative_to(root).as_posix(),
                resolved.stat().st_size,
                _sha256_bytes(resolved.read_bytes()),
            )
        )
    return rows


def test_stage1_fingerprint_matches_frozen_evidence_tree_convention(tmp_path):
    root = tmp_path / "tree"
    nested = root / "nested"
    nested.mkdir(parents=True)
    (root / "a.txt").write_bytes(b"alpha")
    (nested / "b.txt").write_bytes(b"beta")

    identity = g7h.stage1_tree_fingerprint(root)

    assert _frozen_evidence_rows(root) == [
        "a.txt\t5\t" + _sha256_bytes(b"alpha"),
        "nested/b.txt\t4\t" + _sha256_bytes(b"beta"),
    ]
    assert identity["file_count"] == 2
    assert identity["fingerprint"] == (
        "7e156c39bb9d54c3cca24f515d3e34b84cfedbd4398aca05333664ae8040c8af"
    )


def test_stage1_fingerprint_rejects_legacy_windows_column_order(tmp_path):
    root = tmp_path / "tree"
    nested = root / "nested"
    nested.mkdir(parents=True)
    (root / "a.txt").write_bytes(b"alpha")
    (nested / "b.txt").write_bytes(b"beta")

    legacy_rows = [
        "a.txt\t%s\t5" % _sha256_bytes(b"alpha"),
        "nested\\b.txt\t%s\t4" % _sha256_bytes(b"beta"),
    ]
    legacy = _sha256_bytes("\n".join(legacy_rows).encode("utf-8"))

    assert g7h.stage1_tree_fingerprint(root)["fingerprint"] != legacy


def test_unexpected_chapter_output_fails(tmp_path):
    kwargs, _context = _prepare(
        tmp_path, stage1_kwargs={"chapter_output": True}
    )

    with pytest.raises(
        g7h.G7HReadinessError, match="chapter or pending output"
    ):
        g7h.run_readiness(**kwargs)


def test_stage1_progress_already_advanced_fails(tmp_path):
    kwargs, context = _prepare(tmp_path)
    (context["run_root"] / "data" / "progress.json").write_text(
        json.dumps({"completed": [1], "last_completed": 1, "next_chapter": 2}),
        encoding="utf-8",
    )
    refreshed = g7h.stage1_tree_fingerprint(context["root"])
    kwargs["expected_stage1_files"] = refreshed["file_count"]
    kwargs["expected_stage1_fingerprint"] = refreshed["fingerprint"]

    with pytest.raises(g7h.G7HReadinessError, match="pre-Chapter-1"):
        g7h.run_readiness(**kwargs)


def test_missing_run_root_fails(tmp_path):
    import shutil

    kwargs, context = _prepare(tmp_path)
    shutil.rmtree(
        context["root"] / Path(stage1_run_specs()[0]["relative_root"])
    )
    refreshed = g7h.stage1_tree_fingerprint(context["root"])
    kwargs["expected_stage1_files"] = refreshed["file_count"]
    kwargs["expected_stage1_fingerprint"] = refreshed["fingerprint"]

    with pytest.raises(g7h.G7HReadinessError, match="run root missing"):
        g7h.run_readiness(**kwargs)


# ---------------------------------------------------------------------------
# credential contract
# ---------------------------------------------------------------------------


def test_active_secret_encrypted_sha_drift_fails(tmp_path):
    kwargs, _context = _prepare(tmp_path, expected_secret_sha256="0" * 64)

    with pytest.raises(
        g7h.G7HReadinessError, match="active credential encrypted SHA-256"
    ):
        g7h.run_readiness(**kwargs)


def test_active_secret_encrypted_byte_drift_fails(tmp_path):
    kwargs, context = _prepare(tmp_path)
    kwargs["expected_secret_bytes"] = context["secret"].stat().st_size + 5

    with pytest.raises(
        g7h.G7HReadinessError, match="active credential encrypted byte count"
    ):
        g7h.run_readiness(**kwargs)


def test_active_secret_unreadable_fails(tmp_path):
    kwargs, _context = _prepare(tmp_path, dpapi=FakeDpapi(fail_unprotect=True))

    with pytest.raises(g7h.G7HReadinessError, match="not readable"):
        g7h.run_readiness(**kwargs)


def test_active_secret_with_invalid_envelope_fails(tmp_path):
    kwargs, context = _prepare(tmp_path)
    context["secret"].write_bytes(b"not-a-protected-envelope")
    kwargs["expected_secret_sha256"] = _sha256_bytes(
        context["secret"].read_bytes()
    )
    kwargs["expected_secret_bytes"] = context["secret"].stat().st_size

    with pytest.raises(g7h.G7HReadinessError, match="not readable"):
        g7h.run_readiness(**kwargs)


def test_legacy_backup_sha_drift_fails(tmp_path):
    kwargs, _context = _prepare(tmp_path, expected_legacy_sha256="0" * 64)

    with pytest.raises(
        g7h.G7HReadinessError, match="legacy credential backup encrypted SHA-256"
    ):
        g7h.run_readiness(**kwargs)


def test_legacy_backup_byte_drift_fails(tmp_path):
    kwargs, context = _prepare(tmp_path)
    kwargs["expected_legacy_bytes"] = context["legacy"].stat().st_size + 5

    with pytest.raises(
        g7h.G7HReadinessError, match="legacy credential backup encrypted byte"
    ):
        g7h.run_readiness(**kwargs)


def test_legacy_backup_is_never_decrypted(tmp_path):
    dpapi = FakeDpapi()
    kwargs, context = _prepare(tmp_path, dpapi=dpapi)

    g7h.run_readiness(**kwargs)

    assert len(dpapi.unprotect_calls) == 1
    assert dpapi.unprotect_calls[0] == context["secret"].read_bytes()
    assert context["legacy"].read_bytes() not in dpapi.unprotect_calls


def test_invalid_legacy_envelope_still_passes_because_it_is_never_decrypted(
    tmp_path,
):
    dpapi = FakeDpapi()
    kwargs, context = _prepare(tmp_path, dpapi=dpapi)
    # A real decrypt would raise; the doctor must not attempt one.
    context["legacy"].write_bytes(b"opaque-legacy-bytes")
    kwargs["expected_legacy_sha256"] = _sha256_bytes(
        context["legacy"].read_bytes()
    )
    kwargs["expected_legacy_bytes"] = context["legacy"].stat().st_size

    report = g7h.run_readiness(**kwargs)

    assert report["legacy_backup"]["decrypted"] is False
    assert len(dpapi.unprotect_calls) == 1


def test_non_windows_platform_fails_before_secret_access(tmp_path):
    dpapi = FakeDpapi()
    kwargs, _context = _prepare(
        tmp_path, dpapi=dpapi, platform_resolver=lambda: False
    )

    with pytest.raises(g7h.G7HReadinessError, match="Windows DPAPI"):
        g7h.run_readiness(**kwargs)

    assert dpapi.unprotect_calls == []


def test_secret_metadata_drift_fails_before_active_secret_read(tmp_path):
    dpapi = FakeDpapi()
    kwargs, _context = _prepare(
        tmp_path, dpapi=dpapi, expected_secret_sha256="0" * 64
    )

    with pytest.raises(g7h.G7HReadinessError):
        g7h.run_readiness(**kwargs)

    assert dpapi.unprotect_calls == []


# ---------------------------------------------------------------------------
# preflight contract
# ---------------------------------------------------------------------------


def test_default_preflight_runner_uses_g7g_preflight(tmp_path, monkeypatch):
    kwargs, context = _prepare(tmp_path)
    calls = {}

    def fake_run_preflight(**inner):
        calls.update(inner)
        plan = context["plan"]
        return {
            "status": "PREFLIGHT_OK",
            "authorization_required": True,
            "paid_execution_authorized": False,
            "provider_calls": 0,
            "run_index": 1,
            "target_chapter": 1,
            "plan_sha256": plan["plan_sha256"],
            "approval_token": plan["approval_token"],
        }

    monkeypatch.setattr(g7g, "run_preflight", fake_run_preflight)
    kwargs["preflight_runner"] = None

    report = g7h.run_readiness(**kwargs)

    assert report["status"] == "READINESS_OK"
    assert Path(calls["plan_path"]) == context["plan_path"]
    assert calls["expected_plan_sha256"] == context["plan"]["plan_sha256"]
    assert calls["expected_approval_token"] == context["plan"]["approval_token"]
    assert calls["stage1_root"] == Path(context["root"]).resolve()


@pytest.mark.parametrize(
    "overrides",
    [
        {"provider_calls": 1},
        {"paid_execution_authorized": True},
        {"authorization_required": False},
        {"status": "AUTHORIZATION_REQUIRED"},
        {"run_index": 2},
        {"target_chapter": 2},
        {"plan_sha256": "0" * 64},
        {"approval_token": "0" * 64},
    ],
)
def test_preflight_disagreement_fails(tmp_path, overrides):
    plan = _plan()
    kwargs, _context = _prepare(
        tmp_path,
        plan=plan,
        preflight_runner=_raw_preflight_runner(plan, overrides=overrides),
    )

    with pytest.raises(g7h.G7HReadinessError, match="preflight"):
        g7h.run_readiness(**kwargs)


@pytest.mark.parametrize(
    "dropped",
    [
        "status",
        "authorization_required",
        "paid_execution_authorized",
        "provider_calls",
        "run_index",
        "target_chapter",
        "plan_sha256",
        "approval_token",
    ],
)
def test_preflight_missing_required_field_fails(tmp_path, dropped):
    plan = _plan()
    kwargs, _context = _prepare(
        tmp_path,
        plan=plan,
        preflight_runner=_raw_preflight_runner(plan, drop=(dropped,)),
    )

    with pytest.raises(g7h.G7HReadinessError, match="preflight"):
        g7h.run_readiness(**kwargs)


def test_preflight_that_does_not_fail_closed_fails(tmp_path):
    plan = _plan()
    executor = FakeExecutor(overrides={"provider_calls": 1})
    kwargs, _context = _prepare(
        tmp_path,
        plan=plan,
        preflight_runner=_g7g_preflight_runner(executor),
    )

    with pytest.raises(g7h.G7HReadinessError, match="preflight failed"):
        g7h.run_readiness(**kwargs)


def test_preflight_executor_is_called_unauthorized_and_without_a_key(tmp_path):
    executor = FakeExecutor()
    kwargs, _context = _prepare(
        tmp_path, preflight_runner=_g7g_preflight_runner(executor)
    )

    g7h.run_readiness(**kwargs)

    assert len(executor.calls) == 1
    assert executor.calls[0]["operator_authorized"] is False
    assert executor.calls[0]["api_key"] is None


def test_preflight_mapping_that_is_not_a_mapping_fails(tmp_path):
    kwargs, _context = _prepare(
        tmp_path, preflight_runner=lambda **_kwargs: ["not", "a", "mapping"]
    )

    with pytest.raises(g7h.G7HReadinessError, match="no result mapping"):
        g7h.run_readiness(**kwargs)


def test_preflight_exception_is_wrapped_fail_closed(tmp_path):
    class Boom(Exception):
        pass

    def explode(**_kwargs):
        raise Boom("nope")

    kwargs, _context = _prepare(tmp_path, preflight_runner=explode)

    with pytest.raises(g7h.G7HReadinessError, match="preflight failed: Boom"):
        g7h.run_readiness(**kwargs)


# ---------------------------------------------------------------------------
# journal / ledger contract
# ---------------------------------------------------------------------------


def test_malformed_journal_fails(tmp_path):
    kwargs, _context = _prepare(
        tmp_path,
        stage1_kwargs={"journal_payload": '{"status": "started"}'},
    )

    with pytest.raises(
        g7h.G7HReadinessError, match="malformed paid request journal"
    ):
        g7h.run_readiness(**kwargs)


def test_unparsable_journal_fails(tmp_path):
    kwargs, _context = _prepare(
        tmp_path, stage1_kwargs={"journal_payload": "not json at all"}
    )

    with pytest.raises(
        g7h.G7HReadinessError, match="malformed paid request journal"
    ):
        g7h.run_readiness(**kwargs)


@pytest.mark.parametrize("status", ["started", "uncertain"])
def test_started_or_uncertain_journal_fails(tmp_path, status):
    kwargs, _context = _prepare(
        tmp_path,
        stage1_kwargs={"journal_payload": _journal_record(status)},
    )

    with pytest.raises(
        g7h.G7HReadinessError, match="ambiguous paid request journal"
    ):
        g7h.run_readiness(**kwargs)


def test_planned_journal_before_chapter_one_fails(tmp_path):
    kwargs, _context = _prepare(
        tmp_path,
        stage1_kwargs={"journal_payload": _journal_record("planned")},
    )

    with pytest.raises(
        g7h.G7HReadinessError, match="pre-existing paid request journal"
    ):
        g7h.run_readiness(**kwargs)


def test_response_recorded_journal_fails(tmp_path):
    record = _journal_record("response_recorded")
    record["response"] = {"content": "already paid", "stats": {}}
    record["response_sha256"] = _sha_json(record["response"])
    record.pop("record_sha256")
    record["record_sha256"] = _sha_json(record)

    kwargs, _context = _prepare(
        tmp_path, stage1_kwargs={"journal_payload": record}
    )

    with pytest.raises(
        g7h.G7HReadinessError, match="pre-existing paid request journal"
    ):
        g7h.run_readiness(**kwargs)


def test_malformed_ledger_fails(tmp_path):
    kwargs, _context = _prepare(
        tmp_path, stage1_kwargs={"ledger_payload": {"schema_version": 1}}
    )

    with pytest.raises(
        g7h.G7HReadinessError, match="malformed accounting ledger"
    ):
        g7h.run_readiness(**kwargs)


def test_unexpected_valid_ledger_fails(tmp_path):
    kwargs, _context = _prepare(
        tmp_path,
        stage1_kwargs={"ledger_payload": _valid_ledger_document()},
    )

    with pytest.raises(
        g7h.G7HReadinessError,
        match="pre-existing G7F invocation budget ledger",
    ):
        g7h.run_readiness(**kwargs)


def test_empty_journal_directory_is_absent(tmp_path):
    kwargs, context = _prepare(tmp_path)

    report = g7h.run_readiness(**kwargs)

    assert report["paid_artifacts"]["journal"] == "ABSENT"
    assert (context["run_root"] / "data" / "provider_request_journal").is_dir()


# ---------------------------------------------------------------------------
# no Provider construction or transport
# ---------------------------------------------------------------------------


def test_no_provider_client_is_constructed(tmp_path, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("a Provider client was constructed")

    monkeypatch.setattr(provider, "create_provider_client", forbidden)
    monkeypatch.setattr(g7f, "create_provider_client", forbidden)
    kwargs, _context = _prepare(tmp_path)

    report = g7h.run_readiness(**kwargs)

    assert report["provider_calls"] == 0


def test_no_provider_transport_is_called(tmp_path, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Provider transport was called")

    monkeypatch.setattr(provider, "send_chat_completion_once", forbidden)
    monkeypatch.setattr(g7f, "send_chat_completion_once", forbidden)
    kwargs, _context = _prepare(tmp_path)

    report = g7h.run_readiness(**kwargs)

    assert report["provider_calls"] == 0
    assert report["preflight"]["provider_calls"] == 0


# ---------------------------------------------------------------------------
# human output and CLI
# ---------------------------------------------------------------------------


def test_human_summary_contains_required_markers_and_no_secret(tmp_path):
    kwargs, _context = _prepare(tmp_path)

    report = g7h.run_readiness(**kwargs)
    summary = g7h.render_human_summary(report)

    assert "G7H_READINESS=PASS" in summary
    assert "BENCHMARK_PROVIDER_CALLS=0" in summary
    assert "PAID_EXECUTION_AUTHORIZED=NO" in summary
    assert "CHAPTER_1_EXECUTED=NO" in summary
    assert SECRET_KEY not in summary
    assert LEGACY_KEY not in summary


def _argv(kwargs, json_output):
    return [
        "--code-root", str(kwargs["code_root"]),
        "--stage1-root", str(kwargs["stage1_root"]),
        "--plan", str(kwargs["plan_path"]),
        "--expected-plan-sha256", kwargs["expected_plan_sha256"],
        "--expected-approval-token", kwargs["expected_approval_token"],
        "--secret-path", str(kwargs["secret_path"]),
        "--expected-secret-sha256", kwargs["expected_secret_sha256"],
        "--expected-secret-bytes", str(kwargs["expected_secret_bytes"]),
        "--legacy-backup", str(kwargs["legacy_backup"]),
        "--expected-legacy-sha256", kwargs["expected_legacy_sha256"],
        "--expected-legacy-bytes", str(kwargs["expected_legacy_bytes"]),
        "--expected-stage1-files", str(kwargs["expected_stage1_files"]),
        "--expected-stage1-fingerprint",
        kwargs["expected_stage1_fingerprint"],
        "--expected-g7g-sha256", kwargs["expected_g7g_sha256"],
        "--expected-g7f-sha256", kwargs["expected_g7f_sha256"],
        "--json-output", str(json_output),
    ]


def _patch_cli(monkeypatch, dpapi, plan):
    def fake_run_preflight(**_kwargs):
        return {
            "status": "PREFLIGHT_OK",
            "authorization_required": True,
            "paid_execution_authorized": False,
            "provider_calls": 0,
            "run_index": plan["run_index"],
            "target_chapter": plan["target_chapter"],
            "plan_sha256": plan["plan_sha256"],
            "approval_token": plan["approval_token"],
        }

    monkeypatch.setattr(g7g, "run_preflight", fake_run_preflight)
    monkeypatch.setattr(g7g, "load_platform_dpapi", lambda: dpapi)
    monkeypatch.setattr(g7g, "current_platform_is_supported", lambda: True)


def test_cli_writes_json_evidence_and_prints_pass(tmp_path, monkeypatch, capsys):
    dpapi = FakeDpapi()
    kwargs, context = _prepare(tmp_path, dpapi=dpapi)
    _patch_cli(monkeypatch, dpapi, context["plan"])
    json_output = tmp_path / "evidence" / "g7h_readiness.json"

    code = g7h.main(_argv(kwargs, json_output))

    captured = capsys.readouterr()
    assert code == 0
    assert "G7H_READINESS=PASS" in captured.out
    assert "BENCHMARK_PROVIDER_CALLS=0" in captured.out
    assert "PAID_EXECUTION_AUTHORIZED=NO" in captured.out
    assert "CHAPTER_1_EXECUTED=NO" in captured.out
    assert SECRET_KEY not in captured.out
    assert SECRET_KEY not in captured.err

    assert json_output.is_file()
    payload = json_output.read_text(encoding="utf-8")
    report = json.loads(payload)
    assert report["status"] == "READINESS_OK"
    assert SECRET_KEY not in payload
    assert LEGACY_KEY not in payload


def test_cli_failure_prints_fail_and_writes_no_json(
    tmp_path, monkeypatch, capsys
):
    dpapi = FakeDpapi()
    kwargs, context = _prepare(tmp_path, dpapi=dpapi)
    _patch_cli(monkeypatch, dpapi, context["plan"])
    json_output = tmp_path / "evidence" / "g7h_readiness.json"
    argv = _argv(kwargs, json_output)
    argv[argv.index("--expected-plan-sha256") + 1] = "f" * 64

    code = g7h.main(argv)

    captured = capsys.readouterr()
    assert code == 1
    assert "G7H_READINESS=FAIL" in captured.out
    assert "BENCHMARK_PROVIDER_CALLS=0" in captured.out
    assert "PAID_EXECUTION_AUTHORIZED=NO" in captured.out
    assert "CHAPTER_1_EXECUTED=NO" in captured.out
    assert SECRET_KEY not in captured.out
    assert not json_output.exists()
    assert dpapi.unprotect_calls == []


def test_build_parser_exposes_the_exact_flags(tmp_path):
    parser = g7h.build_parser()
    args = parser.parse_args(
        [
            "--code-root", "c",
            "--stage1-root", "s",
            "--plan", "p",
            "--expected-plan-sha256", "a" * 64,
            "--expected-approval-token", "b" * 64,
            "--secret-path", "sec",
            "--expected-secret-sha256", "c" * 64,
            "--expected-secret-bytes", "310",
            "--legacy-backup", "leg",
            "--expected-legacy-sha256", "d" * 64,
            "--expected-legacy-bytes", "813",
            "--expected-stage1-files", "121",
            "--expected-stage1-fingerprint", "e" * 64,
            "--expected-g7g-sha256", "f" * 64,
            "--expected-g7f-sha256", "0" * 64,
            "--json-output", "out.json",
        ]
    )

    assert args.code_root == "c"
    assert args.stage1_root == "s"
    assert args.plan == "p"
    assert args.expected_plan_sha256 == "a" * 64
    assert args.expected_approval_token == "b" * 64
    assert args.secret_path == "sec"
    assert args.expected_secret_sha256 == "c" * 64
    assert args.expected_secret_bytes == 310
    assert args.legacy_backup == "leg"
    assert args.expected_legacy_sha256 == "d" * 64
    assert args.expected_legacy_bytes == 813
    assert args.expected_stage1_files == 121
    assert args.expected_stage1_fingerprint == "e" * 64
    assert args.expected_g7g_sha256 == "f" * 64
    assert args.expected_g7f_sha256 == "0" * 64
    assert args.json_output == "out.json"


def test_parser_has_no_paid_authorization_flag(tmp_path):
    parser = g7h.build_parser()
    base = [
        "--code-root", "c",
        "--stage1-root", "s",
        "--plan", "p",
        "--expected-plan-sha256", "a" * 64,
        "--expected-approval-token", "b" * 64,
        "--secret-path", "sec",
        "--expected-secret-sha256", "c" * 64,
        "--expected-secret-bytes", "310",
        "--legacy-backup", "leg",
        "--expected-legacy-sha256", "d" * 64,
        "--expected-legacy-bytes", "813",
        "--expected-stage1-files", "121",
        "--expected-stage1-fingerprint", "e" * 64,
        "--expected-g7g-sha256", "f" * 64,
        "--expected-g7f-sha256", "0" * 64,
        "--json-output", "out.json",
    ]
    for flag in ("--authorize-paid-run", "--api-key", "--execute"):
        with pytest.raises(SystemExit):
            parser.parse_args(base + [flag, "x"])


def test_main_requires_all_flags(capsys):
    with pytest.raises(SystemExit) as excinfo:
        g7h.main([])

    assert excinfo.value.code == 2


def test_main_help_is_available(capsys):
    with pytest.raises(SystemExit) as excinfo:
        g7h.main(["--help"])

    assert excinfo.value.code == 0
    assert "--json-output" in capsys.readouterr().out
