"""G7G Windows secure launcher around the G7F Stage-1 paid executor.

G7G adds a Windows DPAPI (CurrentUser) secret boundary and a fail-closed
operator workflow (``init-secret``, ``preflight``, ``execute``) on top of the
frozen G7F executor.  Importing this module performs no filesystem writes,
reads no credentials, loads no ``.env`` file, constructs no Provider client,
and makes zero Provider calls.  The real DPAPI backend is lazy and injectable
so the launcher stays testable with fakes.

The launcher never authorizes paid work by itself.  Paid execution requires an
explicit ``--authorize-paid-run`` flag, the exact plan SHA, the exact approval
token, and a successful zero-Provider preflight before any secret access.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import stat
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from benchmark_longitudinal_g7f_stage1_paid_executor import (
    execute_stage1_invocation as _g7f_execute_stage1_invocation,
)

G7G_SCHEMA_VERSION = 1
G7G_REVISION = "goat-g7g-stage1-secure-launcher-v1"
AUTHORIZATION_REQUIRED_STATUS = "AUTHORIZATION_REQUIRED"
LOCALAPPDATA_ENV = "LOCALAPPDATA"
DEFAULT_SECRET_DIR_PARTS = ("NovelTranslator", "Secrets")
DEFAULT_SECRET_FILENAME = "command_code_api_key.dpapi"
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_REDACTED = "[REDACTED]"

DEFAULT_EXECUTOR: Callable[..., Mapping[str, Any]] = (
    _g7f_execute_stage1_invocation
)


class G7GSecureLauncherError(RuntimeError):
    """The G7G secure launcher refused an unsafe operation."""


class UnsupportedPlatformError(G7GSecureLauncherError):
    """The current operating system cannot provide Windows DPAPI."""


class SecretPathError(G7GSecureLauncherError):
    """The requested secret target is an unsafe path."""


class PlanIdentityError(G7GSecureLauncherError):
    """The invocation plan did not match the expected SHA/token."""


def current_platform_is_supported() -> bool:
    """Return True only on a Windows host able to provide DPAPI."""
    return os.name == "nt"


def _assert_supported_platform(
    resolver: Callable[[], bool] | None = None,
) -> None:
    checker = resolver if resolver is not None else current_platform_is_supported
    if not checker():
        raise UnsupportedPlatformError(
            "G7G secure launcher requires Windows DPAPI "
            "(CurrentUser scope)"
        )


def default_secret_path(
    env: Mapping[str, str] | None = None,
) -> Path:
    """Resolve the default DPAPI secret target from %LOCALAPPDATA%."""
    environ = env if env is not None else os.environ
    base = environ.get(LOCALAPPDATA_ENV)
    if not isinstance(base, str) or not base.strip():
        raise G7GSecureLauncherError(
            f"{LOCALAPPDATA_ENV} is required to resolve the default "
            "secret target"
        )
    return Path(base).joinpath(*DEFAULT_SECRET_DIR_PARTS, DEFAULT_SECRET_FILENAME)


def _path_component_is_reparse_or_symlink(path: Path) -> bool:
    try:
        status = path.lstat()
    except OSError:
        return False
    if stat.S_ISLNK(status.st_mode):
        return True
    attributes = getattr(status, "st_file_attributes", 0)
    return bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)


def assert_safe_secret_target(path: Path | str) -> Path:
    """Refuse symlink/reparse secret targets and path components."""
    target = Path(path)
    for component in (target, *target.parents):
        if _path_component_is_reparse_or_symlink(component):
            raise SecretPathError(
                f"unsafe secret path component: {component}"
            )
    return target


class WindowsDpapi:
    """Real DPAPI CurrentUser backend implemented with the stdlib only."""

    def __init__(self, *, ui_forbidden: bool = True):
        if os.name != "nt":
            raise UnsupportedPlatformError(
                "Windows DPAPI is only available on Windows"
            )
        self._flags = 0x1 if ui_forbidden else 0

    def protect(self, plaintext: bytes) -> bytes:
        return self._transform(plaintext, protect=True)

    def unprotect(self, ciphertext: bytes) -> bytes:
        return self._transform(ciphertext, protect=False)

    def _transform(self, data: bytes, *, protect: bool) -> bytes:
        import ctypes
        from ctypes import wintypes

        payload = bytes(data)
        if not payload:
            raise G7GSecureLauncherError(
                "DPAPI input must not be empty"
            )

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [
                ("cbData", wintypes.DWORD),
                ("pbData", ctypes.c_void_p),
            ]

        blob_pointer = ctypes.POINTER(DATA_BLOB)
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        crypt32.CryptProtectData.argtypes = [
            blob_pointer,
            wintypes.LPCWSTR,
            blob_pointer,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            blob_pointer,
        ]
        crypt32.CryptProtectData.restype = wintypes.BOOL
        crypt32.CryptUnprotectData.argtypes = [
            blob_pointer,
            ctypes.POINTER(wintypes.LPWSTR),
            blob_pointer,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            blob_pointer,
        ]
        crypt32.CryptUnprotectData.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p

        buffer = ctypes.create_string_buffer(payload, len(payload))
        blob_in = DATA_BLOB(
            len(payload),
            ctypes.cast(buffer, ctypes.c_void_p),
        )
        blob_out = DATA_BLOB()

        operation = (
            crypt32.CryptProtectData
            if protect
            else crypt32.CryptUnprotectData
        )
        ok = operation(
            ctypes.byref(blob_in),
            None,
            None,
            None,
            None,
            wintypes.DWORD(self._flags),
            ctypes.byref(blob_out),
        )
        if not ok:
            raise G7GSecureLauncherError(
                "DPAPI "
                + ("protect" if protect else "unprotect")
                + " failed with code "
                + str(ctypes.get_last_error())
            )
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            kernel32.LocalFree(blob_out.pbData)


def load_platform_dpapi() -> WindowsDpapi:
    """Construct the real DPAPI backend, failing closed off-Windows."""
    if not current_platform_is_supported():
        raise UnsupportedPlatformError(
            "G7G secure launcher requires Windows DPAPI "
            "(CurrentUser scope)"
        )
    return WindowsDpapi()


def _default_passphrase_reader() -> str:
    return getpass.getpass("Command Code API key (input hidden): ")


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    target = assert_safe_secret_target(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    assert_safe_secret_target(target)
    handle_fd, temp_name = tempfile.mkstemp(
        dir=str(target.parent),
        prefix=".g7g-secret-",
        suffix=".tmp",
    )
    try:
        with os.fdopen(handle_fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def init_secret(
    *,
    secret_path: Path | str | None = None,
    dpapi: Any | None = None,
    passphrase_reader: Callable[[], str] | None = None,
    platform_resolver: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Interactively create a DPAPI-protected secret with no Provider call."""
    _assert_supported_platform(platform_resolver)
    target = assert_safe_secret_target(
        secret_path if secret_path is not None else default_secret_path()
    )
    backend = dpapi if dpapi is not None else load_platform_dpapi()
    reader = (
        passphrase_reader
        if passphrase_reader is not None
        else _default_passphrase_reader
    )

    plaintext = reader()
    if not isinstance(plaintext, str) or not plaintext.strip():
        raise G7GSecureLauncherError("refusing to store an empty secret")
    data = plaintext.encode("utf-8")
    plaintext = None

    ciphertext = backend.protect(data)
    if not isinstance(ciphertext, (bytes, bytearray)) or not ciphertext:
        raise G7GSecureLauncherError("DPAPI returned no ciphertext")
    ciphertext = bytes(ciphertext)
    if ciphertext == data:
        raise G7GSecureLauncherError(
            "DPAPI did not protect the secret bytes"
        )
    if data in ciphertext:
        raise G7GSecureLauncherError(
            "protected ciphertext must not contain plaintext"
        )

    _atomic_write_bytes(target, ciphertext)

    return {
        "status": "SECRET_INITIALIZED",
        "schema_version": G7G_SCHEMA_VERSION,
        "revision": G7G_REVISION,
        "secret_path": str(target),
        "ciphertext_bytes": len(ciphertext),
        "protected_with": "windows_dpapi_current_user",
        "provider_calls": 0,
    }


def load_invocation_plan(plan_path: Path | str) -> dict[str, Any]:
    """Load the external G7F invocation plan JSON object."""
    path = Path(plan_path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise G7GSecureLauncherError(
            f"cannot read invocation plan: {path}"
        ) from exc
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise G7GSecureLauncherError(
            "invocation plan is not valid JSON"
        ) from exc
    if not isinstance(value, dict):
        raise G7GSecureLauncherError(
            "invocation plan must be a JSON object"
        )
    return value


def _require_plan_identity(
    plan: Mapping[str, Any],
    expected_plan_sha256: str,
    expected_approval_token: str,
) -> None:
    stored_sha = plan.get("plan_sha256")
    if (
        not isinstance(expected_plan_sha256, str)
        or not expected_plan_sha256
        or stored_sha != expected_plan_sha256
    ):
        raise PlanIdentityError(
            "invocation plan SHA does not match the expected plan SHA"
        )
    stored_token = plan.get("approval_token")
    if (
        not isinstance(expected_approval_token, str)
        or not expected_approval_token
        or stored_token != expected_approval_token
    ):
        raise PlanIdentityError(
            "invocation plan approval token does not match the "
            "expected token"
        )


def _resolve_executor(
    executor: Callable[..., Mapping[str, Any]] | None,
) -> Callable[..., Mapping[str, Any]]:
    return executor if executor is not None else DEFAULT_EXECUTOR


def _run_preflight_internal(
    *,
    code_root: Path | str,
    stage1_root: Path | str,
    plan_path: Path | str,
    expected_plan_sha256: str,
    expected_approval_token: str,
    executor: Callable[..., Mapping[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load/validate one plan and run the zero-Provider authorization check.

    Returns both the exact validated plan object (so paid execution reuses it
    instead of re-reading the plan file) and the compact public result.
    """
    plan = load_invocation_plan(plan_path)
    _require_plan_identity(
        plan,
        expected_plan_sha256,
        expected_approval_token,
    )

    use_executor = _resolve_executor(executor)
    try:
        result = use_executor(
            code_root=code_root,
            stage1_root=stage1_root,
            plan=plan,
            expected_plan_sha256=expected_plan_sha256,
            expected_approval_token=expected_approval_token,
            operator_authorized=False,
            api_key=None,
        )
    except G7GSecureLauncherError:
        raise
    except BaseException as exc:
        raise G7GSecureLauncherError(
            "preflight executor failed: "
            f"{type(exc).__name__}: {exc}"
        ) from None

    if not isinstance(result, Mapping):
        raise G7GSecureLauncherError(
            "preflight executor returned no result mapping"
        )
    if result.get("status") != AUTHORIZATION_REQUIRED_STATUS:
        raise G7GSecureLauncherError(
            "preflight did not require operator authorization"
        )
    if result.get("provider_calls") != 0:
        raise G7GSecureLauncherError(
            "preflight executor did not report zero Provider calls"
        )
    for field in (
        "plan_sha256",
        "approval_token",
        "run_index",
        "target_chapter",
    ):
        if result.get(field) != plan.get(field):
            raise G7GSecureLauncherError(
                "preflight executor disagreed with the validated plan: "
                f"{field}"
            )

    public = {
        "status": "PREFLIGHT_OK",
        "schema_version": G7G_SCHEMA_VERSION,
        "revision": G7G_REVISION,
        "authorization_required": True,
        "paid_execution_authorized": False,
        "plan_sha256": plan.get("plan_sha256"),
        "approval_token": plan.get("approval_token"),
        "run_index": result.get("run_index"),
        "target_chapter": result.get("target_chapter"),
        "provider_calls": 0,
    }
    return plan, public


def run_preflight(
    *,
    code_root: Path | str,
    stage1_root: Path | str,
    plan_path: Path | str,
    expected_plan_sha256: str,
    expected_approval_token: str,
    executor: Callable[..., Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run the zero-Provider authorization check, reading no secret."""
    _plan, record = _run_preflight_internal(
        code_root=code_root,
        stage1_root=stage1_root,
        plan_path=plan_path,
        expected_plan_sha256=expected_plan_sha256,
        expected_approval_token=expected_approval_token,
        executor=executor,
    )
    return record


def _read_secret_key(
    path: Path | str,
    dpapi: Any,
) -> str:
    target = assert_safe_secret_target(path)
    try:
        ciphertext = target.read_bytes()
    except OSError as exc:
        raise G7GSecureLauncherError(
            f"cannot read protected secret: {target}"
        ) from exc
    if not ciphertext:
        raise G7GSecureLauncherError("protected secret is empty")

    try:
        plaintext = dpapi.unprotect(ciphertext)
    except G7GSecureLauncherError:
        raise
    except BaseException as exc:
        raise G7GSecureLauncherError(
            "DPAPI could not unprotect the stored secret"
        ) from None

    if isinstance(plaintext, str):
        text = plaintext
    elif isinstance(plaintext, (bytes, bytearray)):
        try:
            text = bytes(plaintext).decode("utf-8")
        except UnicodeDecodeError:
            raise G7GSecureLauncherError(
                "unprotected secret is not valid UTF-8"
            ) from None
    else:
        raise G7GSecureLauncherError(
            "DPAPI returned an unsupported secret type"
        )

    text = text.strip()
    if not text:
        raise G7GSecureLauncherError("unprotected secret is empty")
    return text


def _sanitize_text(text: str, secrets: Sequence[str]) -> str:
    result = text
    for secret in secrets:
        if secret:
            result = result.replace(secret, _REDACTED)
    return result


def _sanitize_mapping_key(key: Any, secrets: Sequence[str]) -> Any:
    if isinstance(key, str):
        return _sanitize_text(key, secrets)
    if isinstance(key, (bytes, bytearray)):
        raw = bytes(key)
        if any(secret and secret.encode("utf-8") in raw for secret in secrets):
            return _REDACTED
    return key


def _sanitize_value(value: Any, secrets: Sequence[str]) -> Any:
    if isinstance(value, str):
        return _sanitize_text(value, secrets)
    if isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
        if any(secret and secret.encode("utf-8") in raw for secret in secrets):
            return _REDACTED
        return value
    if isinstance(value, Mapping):
        return {
            _sanitize_mapping_key(key, secrets): _sanitize_value(
                item, secrets
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize_value(item, secrets) for item in value]
    return value


def _sanitize_exception(
    exc: BaseException,
    secrets: Sequence[str],
) -> G7GSecureLauncherError:
    message = _sanitize_text(str(exc), secrets)
    return G7GSecureLauncherError(
        "paid execution failed: "
        f"{type(exc).__name__}: {message}"
    )


def _require_paid_result(
    result: Mapping[str, Any],
    plan: Mapping[str, Any],
) -> None:
    """Fail closed unless the paid result agrees with the preflighted plan."""
    if result.get("status") != "COMPLETED":
        raise G7GSecureLauncherError(
            "paid executor did not report a COMPLETED status"
        )
    if result.get("paid_execution_authorized") is not True:
        raise G7GSecureLauncherError(
            "paid executor did not report paid execution authorization"
        )
    if result.get("automatic_chapter_advance") is not False:
        raise G7GSecureLauncherError(
            "paid executor did not report single-chapter advance policy"
        )
    for field in (
        "plan_sha256",
        "approval_token",
        "run_index",
        "target_chapter",
    ):
        if result.get(field) != plan.get(field):
            raise G7GSecureLauncherError(
                "paid executor disagreed with the preflighted plan: "
                f"{field}"
            )
    if "relative_root" in plan and (
        result.get("relative_root") != plan.get("relative_root")
    ):
        raise G7GSecureLauncherError(
            "paid executor disagreed with the preflighted plan: "
            "relative_root"
        )


def run_execute(
    *,
    code_root: Path | str,
    stage1_root: Path | str,
    plan_path: Path | str,
    expected_plan_sha256: str,
    expected_approval_token: str,
    authorize_paid_run: bool,
    secret_path: Path | str | None = None,
    dpapi: Any | None = None,
    executor: Callable[..., Mapping[str, Any]] | None = None,
    platform_resolver: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Run one authorized invocation after a zero-Provider preflight."""
    if authorize_paid_run is not True:
        raise G7GSecureLauncherError(
            "paid execution requires the explicit "
            "--authorize-paid-run flag"
        )

    plan, preflight = _run_preflight_internal(
        code_root=code_root,
        stage1_root=stage1_root,
        plan_path=plan_path,
        expected_plan_sha256=expected_plan_sha256,
        expected_approval_token=expected_approval_token,
        executor=executor,
    )

    _assert_supported_platform(platform_resolver)
    target = (
        secret_path if secret_path is not None else default_secret_path()
    )
    backend = dpapi if dpapi is not None else load_platform_dpapi()

    api_key = _read_secret_key(target, backend)
    redactors = [api_key]
    try:
        use_executor = _resolve_executor(executor)
        try:
            result = use_executor(
                code_root=code_root,
                stage1_root=stage1_root,
                plan=plan,
                expected_plan_sha256=expected_plan_sha256,
                expected_approval_token=expected_approval_token,
                operator_authorized=True,
                api_key=api_key,
            )
        except BaseException as exc:
            raise _sanitize_exception(exc, redactors) from None
        if not isinstance(result, Mapping):
            raise G7GSecureLauncherError(
                "paid executor returned no result mapping"
            )
        _require_paid_result(result, plan)
        sanitized = _sanitize_value(dict(result), redactors)
    finally:
        api_key = None

    sanitized["preflight"] = preflight.get("status")
    return sanitized


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="g7g-secure-launcher",
        description=(
            "G7G Windows secure launcher for the G7F Stage-1 paid executor"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser(
        "init-secret",
        help="interactively create a DPAPI-protected API key",
    )
    init_parser.add_argument("--secret-path", default=None)

    preflight_parser = subparsers.add_parser(
        "preflight",
        help="run the zero-Provider authorization check",
    )
    _add_invocation_arguments(preflight_parser)

    execute_parser = subparsers.add_parser(
        "execute",
        help="run one authorized invocation",
    )
    _add_invocation_arguments(execute_parser)
    execute_parser.add_argument(
        "--authorize-paid-run",
        action="store_true",
        help="explicitly authorize exactly one paid invocation",
    )
    execute_parser.add_argument("--secret-path", default=None)

    return parser


def _add_invocation_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--code-root", required=True)
    parser.add_argument("--stage1-root", required=True)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--expected-plan-sha256", required=True)
    parser.add_argument("--expected-approval-token", required=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "init-secret":
            result = init_secret(secret_path=args.secret_path)
        elif args.command == "preflight":
            result = run_preflight(
                code_root=args.code_root,
                stage1_root=args.stage1_root,
                plan_path=args.plan,
                expected_plan_sha256=args.expected_plan_sha256,
                expected_approval_token=args.expected_approval_token,
            )
        elif args.command == "execute":
            result = run_execute(
                code_root=args.code_root,
                stage1_root=args.stage1_root,
                plan_path=args.plan,
                expected_plan_sha256=args.expected_plan_sha256,
                expected_approval_token=args.expected_approval_token,
                authorize_paid_run=args.authorize_paid_run,
                secret_path=args.secret_path,
            )
        else:  # pragma: no cover - argparse enforces the subcommand
            parser.error(f"unknown command: {args.command}")
            return 2
    except G7GSecureLauncherError as exc:
        print(
            json.dumps(
                {
                    "status": "ERROR",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


__all__ = [
    "AUTHORIZATION_REQUIRED_STATUS",
    "DEFAULT_EXECUTOR",
    "DEFAULT_SECRET_FILENAME",
    "G7G_REVISION",
    "G7G_SCHEMA_VERSION",
    "G7GSecureLauncherError",
    "PlanIdentityError",
    "SecretPathError",
    "UnsupportedPlatformError",
    "WindowsDpapi",
    "assert_safe_secret_target",
    "build_parser",
    "current_platform_is_supported",
    "default_secret_path",
    "init_secret",
    "load_invocation_plan",
    "load_platform_dpapi",
    "main",
    "run_execute",
    "run_preflight",
]


if __name__ == "__main__":
    raise SystemExit(main())
