import json
import os
from pathlib import Path

import pytest

import benchmark_longitudinal_g7g_stage1_secure_launcher as g7g


SECRET_KEY = "sk-test-command-code-secret-0123456789abcdef"
PROTECTED_PREFIX = b"g7g-protected:"


class FakeDpapi:
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


def _paid_envelope(plan, **overrides):
    result = {
        "status": "COMPLETED",
        "paid_execution_authorized": True,
        "plan_sha256": plan["plan_sha256"],
        "approval_token": plan["approval_token"],
        "run_index": plan["run_index"],
        "relative_root": plan.get("relative_root"),
        "target_chapter": plan["target_chapter"],
        "new_provider_sends_started": 1,
        "accepted_credit_equivalent_usd": "0.0123",
        "budget_ledger_path": "data/g7f_invocation_ledgers/0001.json",
        "automatic_chapter_advance": False,
    }
    result.update(overrides)
    return result


class FakeExecutor:
    def __init__(
        self,
        *,
        paid_result=None,
        paid_overrides=None,
        paid_drop=(),
        authorized_error=None,
        preflight_status=g7g.AUTHORIZATION_REQUIRED_STATUS,
        preflight_overrides=None,
        preflight_drop=(),
    ):
        self.calls = []
        self.paid_result = paid_result
        self.paid_overrides = dict(paid_overrides or {})
        self.paid_drop = tuple(paid_drop)
        self.authorized_error = authorized_error
        self.preflight_status = preflight_status
        self.preflight_overrides = dict(preflight_overrides or {})
        self.preflight_drop = tuple(preflight_drop)

    def _valid_paid_result(self, plan):
        result = _paid_envelope(plan)
        result.update(self.paid_overrides)
        for field in self.paid_drop:
            result.pop(field, None)
        return result

    def _preflight_result(self, plan):
        result = {
            "status": self.preflight_status,
            "paid_execution_authorized": False,
            "plan_sha256": plan.get("plan_sha256"),
            "approval_token": plan.get("approval_token"),
            "run_index": plan.get("run_index"),
            "target_chapter": plan.get("target_chapter"),
            "provider_calls": 0,
        }
        result.update(self.preflight_overrides)
        for field in self.preflight_drop:
            result.pop(field, None)
        return result

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        plan = kwargs["plan"]
        if kwargs.get("operator_authorized"):
            if self.authorized_error is not None:
                raise self.authorized_error
            if self.paid_result is not None:
                result = dict(self.paid_result)
            else:
                result = self._valid_paid_result(plan)
            result.setdefault("echo_api_key", kwargs.get("api_key"))
            return result
        return self._preflight_result(plan)


def _plan(
    *,
    target=1,
    token="a" * 64,
    sha="b" * 64,
    relative_root="g7c/stage1/run-01",
):
    return {
        "schema_version": 1,
        "revision": "goat-g7f-stage1-paid-executor-v2",
        "run_index": 1,
        "target_chapter": target,
        "relative_root": relative_root,
        "approval_token": token,
        "plan_sha256": sha,
    }


def _write_plan(tmp_path: Path, plan) -> Path:
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    return path


def _write_secret(tmp_path: Path, dpapi) -> Path:
    path = tmp_path / "command_code_api_key.dpapi"
    path.write_bytes(dpapi.protect(SECRET_KEY.encode("utf-8")))
    return path


def _execute(tmp_path, executor, dpapi, *, plan=None, **overrides):
    plan = plan if plan is not None else _plan()
    plan_path = _write_plan(tmp_path, plan)
    secret_path = overrides.pop(
        "secret_path", _write_secret(tmp_path, dpapi)
    )
    kwargs = {
        "code_root": tmp_path,
        "stage1_root": tmp_path,
        "plan_path": plan_path,
        "expected_plan_sha256": plan["plan_sha256"],
        "expected_approval_token": plan["approval_token"],
        "authorize_paid_run": True,
        "secret_path": secret_path,
        "dpapi": dpapi,
        "executor": executor,
        "platform_resolver": lambda: True,
    }
    kwargs.update(overrides)
    return g7g.run_execute(**kwargs)


def _preflight(tmp_path, executor, *, plan=None, expected_sha=None, expected_token=None):
    plan = plan if plan is not None else _plan()
    plan_path = _write_plan(tmp_path, plan)
    return g7g.run_preflight(
        code_root=tmp_path,
        stage1_root=tmp_path,
        plan_path=plan_path,
        expected_plan_sha256=(
            expected_sha if expected_sha is not None else plan["plan_sha256"]
        ),
        expected_approval_token=(
            expected_token
            if expected_token is not None
            else plan["approval_token"]
        ),
        executor=executor,
    )


def test_preflight_reads_no_secret_and_uses_unauthorized_executor(
    tmp_path, monkeypatch
):
    plan = _plan()
    plan_path = _write_plan(tmp_path, plan)
    executor = FakeExecutor()

    def forbidden_dpapi():
        raise AssertionError("preflight must not construct DPAPI")

    monkeypatch.setattr(g7g, "load_platform_dpapi", forbidden_dpapi)

    result = g7g.run_preflight(
        code_root=tmp_path,
        stage1_root=tmp_path,
        plan_path=plan_path,
        expected_plan_sha256=plan["plan_sha256"],
        expected_approval_token=plan["approval_token"],
        executor=executor,
    )

    assert result["status"] == "PREFLIGHT_OK"
    assert result["provider_calls"] == 0
    assert result["paid_execution_authorized"] is False
    assert len(executor.calls) == 1
    assert executor.calls[0]["operator_authorized"] is False
    assert executor.calls[0]["api_key"] is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"provider_calls": 1},
        {"provider_calls": None},
        {"plan_sha256": "c" * 64},
        {"approval_token": "c" * 64},
        {"run_index": 99},
        {"target_chapter": 99},
    ],
)
def test_preflight_rejects_zero_provider_or_identity_mismatch(
    tmp_path, overrides
):
    executor = FakeExecutor(preflight_overrides=overrides)

    with pytest.raises(g7g.G7GSecureLauncherError):
        _preflight(tmp_path, executor)


@pytest.mark.parametrize(
    "dropped",
    ["provider_calls", "plan_sha256", "approval_token", "run_index", "target_chapter"],
)
def test_preflight_rejects_missing_required_fields(tmp_path, dropped):
    executor = FakeExecutor(preflight_drop=(dropped,))

    with pytest.raises(g7g.G7GSecureLauncherError):
        _preflight(tmp_path, executor)


def test_wrong_plan_sha_fails_before_secret_access(tmp_path):
    dpapi = FakeDpapi()
    executor = FakeExecutor()

    with pytest.raises(g7g.PlanIdentityError):
        _execute(
            tmp_path,
            executor,
            dpapi,
            expected_plan_sha256="c" * 64,
        )

    assert executor.calls == []
    assert dpapi.unprotect_calls == []


def test_wrong_approval_token_fails_before_secret_access(tmp_path):
    dpapi = FakeDpapi()
    executor = FakeExecutor()

    with pytest.raises(g7g.PlanIdentityError):
        _execute(
            tmp_path,
            executor,
            dpapi,
            expected_approval_token="c" * 64,
        )

    assert executor.calls == []
    assert dpapi.unprotect_calls == []


def test_missing_authorize_paid_run_fails_before_secret_access(tmp_path):
    dpapi = FakeDpapi()
    executor = FakeExecutor()

    with pytest.raises(g7g.G7GSecureLauncherError, match="authorize-paid-run"):
        _execute(tmp_path, executor, dpapi, authorize_paid_run=False)

    assert executor.calls == []
    assert dpapi.unprotect_calls == []


def test_init_secret_never_exposes_plaintext(tmp_path, capsys):
    dpapi = FakeDpapi()
    secret = tmp_path / "secrets" / "command_code_api_key.dpapi"

    result = g7g.init_secret(
        secret_path=secret,
        dpapi=dpapi,
        passphrase_reader=lambda: SECRET_KEY,
        platform_resolver=lambda: True,
    )

    captured = capsys.readouterr()
    rendered = json.dumps(result)
    assert SECRET_KEY not in rendered
    assert SECRET_KEY not in captured.out
    assert SECRET_KEY not in captured.err
    assert result["status"] == "SECRET_INITIALIZED"
    assert result["provider_calls"] == 0


def test_init_secret_persists_only_protected_bytes(tmp_path):
    dpapi = FakeDpapi()
    secret = tmp_path / "command_code_api_key.dpapi"

    g7g.init_secret(
        secret_path=secret,
        dpapi=dpapi,
        passphrase_reader=lambda: SECRET_KEY,
        platform_resolver=lambda: True,
    )

    data = secret.read_bytes()
    assert data.startswith(PROTECTED_PREFIX)
    assert data != SECRET_KEY.encode("utf-8")
    assert SECRET_KEY.encode("utf-8") not in data


def test_init_secret_refuses_empty_secret(tmp_path):
    dpapi = FakeDpapi()
    secret = tmp_path / "command_code_api_key.dpapi"

    with pytest.raises(g7g.G7GSecureLauncherError, match="empty secret"):
        g7g.init_secret(
            secret_path=secret,
            dpapi=dpapi,
            passphrase_reader=lambda: "   ",
            platform_resolver=lambda: True,
        )

    assert not secret.exists()
    assert dpapi.protect_calls == []


def test_unsupported_os_fails_closed_before_real_dpapi(tmp_path):
    secret = tmp_path / "command_code_api_key.dpapi"

    with pytest.raises(g7g.UnsupportedPlatformError):
        g7g.init_secret(
            secret_path=secret,
            passphrase_reader=lambda: SECRET_KEY,
            platform_resolver=lambda: False,
        )

    assert not secret.exists()


def test_execute_unsupported_os_fails_closed_before_secret_access(tmp_path):
    dpapi = FakeDpapi()
    executor = FakeExecutor()
    secret = _write_secret(tmp_path, dpapi)

    with pytest.raises(g7g.UnsupportedPlatformError):
        _execute(
            tmp_path,
            executor,
            dpapi,
            secret_path=secret,
            platform_resolver=lambda: False,
        )

    assert dpapi.unprotect_calls == []


def test_corrupted_protected_secret_fails_closed(tmp_path):
    dpapi = FakeDpapi(fail_unprotect=True)
    executor = FakeExecutor()
    secret = tmp_path / "command_code_api_key.dpapi"
    secret.write_bytes(b"g7g-protected:not-a-real-envelope")

    with pytest.raises(g7g.G7GSecureLauncherError):
        _execute(tmp_path, executor, dpapi, secret_path=secret)

    assert [c for c in executor.calls if c["operator_authorized"]] == []


def test_preflight_occurs_before_unprotect_and_paid_call(tmp_path):
    events: list[str] = []

    class OrderingExecutor(FakeExecutor):
        def __call__(self, **kwargs):
            events.append(
                "paid:authorized" if kwargs.get("operator_authorized")
                else "preflight"
            )
            return super().__call__(**kwargs)

    class OrderingDpapi(FakeDpapi):
        def unprotect(self, ciphertext):
            events.append("unprotect")
            return super().unprotect(ciphertext)

    executor = OrderingExecutor()
    dpapi = OrderingDpapi()

    _execute(tmp_path, executor, dpapi)

    assert events == ["preflight", "unprotect", "paid:authorized"]


def test_unprotect_skipped_when_preflight_not_authorization_required(tmp_path):
    dpapi = FakeDpapi()
    executor = FakeExecutor(preflight_status="COMPLETED")

    with pytest.raises(g7g.G7GSecureLauncherError, match="operator authorization"):
        _execute(tmp_path, executor, dpapi)

    assert dpapi.unprotect_calls == []


def test_key_passed_only_as_in_memory_api_key(tmp_path):
    dpapi = FakeDpapi()
    executor = FakeExecutor()

    result = _execute(tmp_path, executor, dpapi)

    preflight_calls = [c for c in executor.calls if not c["operator_authorized"]]
    paid_calls = [c for c in executor.calls if c["operator_authorized"]]
    assert len(preflight_calls) == 1
    assert len(paid_calls) == 1
    assert preflight_calls[0]["operator_authorized"] is False
    assert preflight_calls[0]["api_key"] is None
    assert paid_calls[0]["operator_authorized"] is True
    assert paid_calls[0]["api_key"] == SECRET_KEY

    rendered = json.dumps(result)
    assert SECRET_KEY not in rendered
    assert g7g._REDACTED in rendered
    assert result["paid_execution_authorized"] is True
    assert result["automatic_chapter_advance"] is False


def test_execute_reuses_the_exact_preflighted_plan_object(tmp_path, monkeypatch):
    plan = _plan()
    plan_path = _write_plan(tmp_path, plan)
    dpapi = FakeDpapi()
    secret = _write_secret(tmp_path, dpapi)

    reads: list[Path] = []
    real_load = g7g.load_invocation_plan

    def counting_load(path):
        reads.append(Path(path))
        return real_load(path)

    monkeypatch.setattr(g7g, "load_invocation_plan", counting_load)

    paid_plans: list = []

    class ReplacingExecutor(FakeExecutor):
        def __call__(self, **kwargs):
            if not kwargs.get("operator_authorized"):
                plan_path.write_text(
                    json.dumps(_plan(token="z" * 64, sha="z" * 64)),
                    encoding="utf-8",
                )
            else:
                paid_plans.append(kwargs["plan"])
            return super().__call__(**kwargs)

    executor = ReplacingExecutor()

    result = g7g.run_execute(
        code_root=tmp_path,
        stage1_root=tmp_path,
        plan_path=plan_path,
        expected_plan_sha256=plan["plan_sha256"],
        expected_approval_token=plan["approval_token"],
        authorize_paid_run=True,
        secret_path=secret,
        dpapi=dpapi,
        executor=executor,
        platform_resolver=lambda: True,
    )

    assert len(reads) == 1
    assert len(paid_plans) == 1
    assert paid_plans[0] == plan
    assert result["preflight"] == "PREFLIGHT_OK"


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "FAILED"},
        {"status": None},
        {"paid_execution_authorized": False},
        {"paid_execution_authorized": None},
        {"automatic_chapter_advance": True},
        {"automatic_chapter_advance": None},
        {"plan_sha256": "c" * 64},
        {"approval_token": "c" * 64},
        {"run_index": 99},
        {"target_chapter": 99},
        {"relative_root": "g7c/stage1/run-99"},
    ],
)
def test_malformed_paid_results_fail_closed(tmp_path, overrides):
    dpapi = FakeDpapi()
    executor = FakeExecutor(paid_overrides=overrides)

    with pytest.raises(g7g.G7GSecureLauncherError):
        _execute(tmp_path, executor, dpapi)


@pytest.mark.parametrize(
    "dropped",
    [
        "status",
        "paid_execution_authorized",
        "plan_sha256",
        "approval_token",
        "run_index",
        "target_chapter",
        "relative_root",
        "automatic_chapter_advance",
    ],
)
def test_partial_paid_results_fail_closed(tmp_path, dropped):
    dpapi = FakeDpapi()
    executor = FakeExecutor(paid_drop=(dropped,))

    with pytest.raises(g7g.G7GSecureLauncherError):
        _execute(tmp_path, executor, dpapi)


def test_manufactured_status_is_not_inserted_when_paid_result_is_incomplete(
    tmp_path,
):
    dpapi = FakeDpapi()
    executor = FakeExecutor(paid_result={"status": "COMPLETED"})

    with pytest.raises(g7g.G7GSecureLauncherError):
        _execute(tmp_path, executor, dpapi)


def test_downstream_exception_containing_key_is_sanitized(tmp_path):
    dpapi = FakeDpapi()
    executor = FakeExecutor(
        authorized_error=RuntimeError(f"transport blew up for {SECRET_KEY}")
    )

    with pytest.raises(g7g.G7GSecureLauncherError) as excinfo:
        _execute(tmp_path, executor, dpapi)

    message = str(excinfo.value)
    assert SECRET_KEY not in message
    assert g7g._REDACTED in message


def test_paid_result_redacts_secret_in_nested_values_and_keys(tmp_path):
    dpapi = FakeDpapi()
    executor = FakeExecutor(
        paid_overrides={
            "nested": {"outer": SECRET_KEY},
            "deep": [{"inner": SECRET_KEY}],
            SECRET_KEY: "must-not-leak",
        }
    )

    result = _execute(tmp_path, executor, dpapi)

    rendered = json.dumps(result)
    assert SECRET_KEY not in rendered
    assert result["nested"]["outer"] == g7g._REDACTED
    assert result["deep"][0]["inner"] == g7g._REDACTED
    assert result[g7g._REDACTED] == "must-not-leak"


def test_no_environment_api_key_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "env-key-must-be-ignored")
    monkeypatch.setenv("OPENAI_API_KEY", "env-key-must-be-ignored")
    monkeypatch.setenv("COMMAND_CODE_API_KEY", "env-key-must-be-ignored")

    dpapi = FakeDpapi()
    executor = FakeExecutor()

    result = _execute(tmp_path, executor, dpapi)

    paid_calls = [c for c in executor.calls if c["operator_authorized"]]
    assert paid_calls[0]["api_key"] == SECRET_KEY
    assert "env-key-must-be-ignored" not in json.dumps(result)


def test_launcher_has_no_agent_or_cmdc_inference_path():
    source = Path(g7g.__file__).read_text(encoding="utf-8").lower()
    for token in ("cmdc", "hermes", "dotenv", "load_dotenv", "subprocess"):
        assert token not in source, token


def test_no_openai_import_in_launcher():
    source = Path(g7g.__file__).read_text(encoding="utf-8")
    assert "openai" not in source


def test_secret_path_safety_rejects_reparse_component(tmp_path, monkeypatch):
    secret = tmp_path / "command_code_api_key.dpapi"
    monkeypatch.setattr(
        g7g,
        "_path_component_is_reparse_or_symlink",
        lambda path: Path(path) == secret,
    )

    with pytest.raises(g7g.SecretPathError):
        g7g.init_secret(
            secret_path=secret,
            dpapi=FakeDpapi(),
            passphrase_reader=lambda: SECRET_KEY,
            platform_resolver=lambda: True,
        )


def test_secret_path_safety_rejects_real_symlink(tmp_path):
    target = tmp_path / "real.dpapi"
    target.write_bytes(b"protected")
    link = tmp_path / "link.dpapi"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not permitted on this host")

    with pytest.raises(g7g.SecretPathError):
        g7g.assert_safe_secret_target(link)


def test_atomic_write_rejects_unsafe_ancestor_before_creating_directories(
    tmp_path, monkeypatch
):
    ancestor = tmp_path / "unsafe-ancestor"
    ancestor.mkdir()
    target = ancestor / "nested" / "command_code_api_key.dpapi"
    monkeypatch.setattr(
        g7g,
        "_path_component_is_reparse_or_symlink",
        lambda path: Path(path) == ancestor,
    )

    with pytest.raises(g7g.SecretPathError):
        g7g._atomic_write_bytes(target, b"ciphertext")

    assert not (ancestor / "nested").exists()
    assert not target.exists()


def test_default_secret_path_uses_localappdata(monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\tester\AppData\Local")

    path = g7g.default_secret_path()

    assert path.name == "command_code_api_key.dpapi"
    assert path.parent.name == "Secrets"
    assert path.parent.parent.name == "NovelTranslator"
    assert path.parts[0].startswith("C:")


def test_default_secret_path_requires_localappdata(monkeypatch):
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    with pytest.raises(g7g.G7GSecureLauncherError):
        g7g.default_secret_path()


def test_module_has_executable_main_guard():
    source = Path(g7g.__file__).read_text(encoding="utf-8")
    assert 'if __name__ == "__main__":' in source
    assert "raise SystemExit(main())" in source


def test_main_help_path_is_present(capsys):
    with pytest.raises(SystemExit) as excinfo:
        g7g.main(["--help"])

    assert excinfo.value.code == 0
    assert "preflight" in capsys.readouterr().out


def test_main_requires_a_subcommand(capsys):
    with pytest.raises(SystemExit) as excinfo:
        g7g.main([])

    assert excinfo.value.code == 2


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI is Windows-only")
def test_real_windows_dpapi_round_trip():
    backend = g7g.WindowsDpapi()
    payload = b"NovelTranslator-G7G-DPAPI-smoke-v1"

    ciphertext = backend.protect(payload)

    assert isinstance(ciphertext, bytes)
    assert ciphertext
    assert ciphertext != payload
    assert payload not in ciphertext
    assert backend.unprotect(ciphertext) == payload


def test_cli_exposes_three_subcommands_and_no_key_option(tmp_path):
    parser = g7g.build_parser()

    args = parser.parse_args(
        [
            "execute",
            "--code-root",
            str(tmp_path),
            "--stage1-root",
            str(tmp_path),
            "--plan",
            str(tmp_path / "plan.json"),
            "--expected-plan-sha256",
            "a" * 64,
            "--expected-approval-token",
            "b" * 64,
            "--authorize-paid-run",
        ]
    )
    assert args.command == "execute"
    assert args.authorize_paid_run is True

    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "execute",
                "--code-root",
                str(tmp_path),
                "--stage1-root",
                str(tmp_path),
                "--plan",
                str(tmp_path / "plan.json"),
                "--expected-plan-sha256",
                "a" * 64,
                "--expected-approval-token",
                "b" * 64,
                "--api-key",
                "plaintext-key",
            ]
        )
