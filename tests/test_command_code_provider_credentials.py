"""Zero-provider tests for the Command Code Provider credential seam.

The real DPAPI secret is never read: every test injects a fake DPAPI backend
and a fake secret path.
"""
import os

import pytest

import command_code_provider_credentials as creds


class _FakeDpapi:
    def __init__(self, plaintext):
        self._plaintext = plaintext

    def unprotect(self, ciphertext):
        return self._plaintext


def _write_secret(tmp_path, payload=b"dpapi-ciphertext"):
    path = tmp_path / "command_code_api_key.dpapi"
    path.write_bytes(payload)
    return path


def test_read_api_key_roundtrip_bytes(tmp_path):
    path = _write_secret(tmp_path)
    key = creds.read_command_code_api_key(path, dpapi=_FakeDpapi(b"sk-cc-fake-key\n"))
    assert key == "sk-cc-fake-key"


def test_read_api_key_roundtrip_str(tmp_path):
    path = _write_secret(tmp_path)
    key = creds.read_command_code_api_key(path, dpapi=_FakeDpapi("  sk-cc-fake-key  "))
    assert key == "sk-cc-fake-key"


def test_missing_secret_fails_closed_without_leaking_plaintext(tmp_path):
    with pytest.raises(creds.CommandCodeCredentialError) as excinfo:
        creds.read_command_code_api_key(
            tmp_path / "nope.dpapi", dpapi=_FakeDpapi(b"sk-cc-fake-key")
        )
    assert "sk-cc-fake-key" not in str(excinfo.value)


def test_empty_secret_fails_closed(tmp_path):
    path = _write_secret(tmp_path, payload=b"")
    with pytest.raises(creds.CommandCodeCredentialError):
        creds.read_command_code_api_key(path, dpapi=_FakeDpapi(b"sk-cc-fake-key"))


def test_environment_sets_and_clears(monkeypatch, tmp_path):
    monkeypatch.delenv(creds.COMMAND_CODE_API_KEY_ENV, raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    path = _write_secret(tmp_path)
    with creds.command_code_api_key_environment(
        secret_path=path, dpapi=_FakeDpapi(b"sk-cc-fake-key")
    ) as info:
        assert os.environ[creds.COMMAND_CODE_API_KEY_ENV] == "sk-cc-fake-key"
        assert info["source"] == "command_code_dpapi"
        # never becomes DEEPSEEK_API_KEY
        assert os.environ.get("DEEPSEEK_API_KEY") is None
    assert creds.COMMAND_CODE_API_KEY_ENV not in os.environ


def test_environment_restores_previous_value(monkeypatch, tmp_path):
    monkeypatch.setenv(creds.COMMAND_CODE_API_KEY_ENV, "prior")
    path = _write_secret(tmp_path)
    with creds.command_code_api_key_environment(
        secret_path=path, dpapi=_FakeDpapi(b"sk-cc-fake-key")
    ):
        assert os.environ[creds.COMMAND_CODE_API_KEY_ENV] == "sk-cc-fake-key"
    assert os.environ[creds.COMMAND_CODE_API_KEY_ENV] == "prior"


def test_redact_secret():
    text = "report contains sk-cc-fake-key in it"
    assert "sk-cc-fake-key" not in creds.redact_secret(text, "sk-cc-fake-key")
    assert "[REDACTED]" in creds.redact_secret(text, "sk-cc-fake-key")


def test_default_path_requires_localappdata():
    with pytest.raises(Exception):
        creds.command_code_secret_path(env={})


def test_import_is_inert(monkeypatch):
    import socket
    import sys
    from pathlib import Path
    from types import ModuleType

    source = Path(creds.__file__).read_text(encoding="utf-8")
    module = ModuleType("_test_inert_credentials")
    monkeypatch.setitem(sys.modules, module.__name__, module)

    def forbidden(*args, **kwargs):
        raise AssertionError("credential module import must not touch network/env")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(os, "getenv", forbidden)
    exec(compile(source, creds.__file__, "exec"), module.__dict__)
    assert module.COMMAND_CODE_API_KEY_ENV == "COMMAND_CODE_API_KEY"
    assert creds.COMMAND_CODE_API_KEY_ENV not in os.environ
    assert "DEEPSEEK_API_KEY" not in os.environ
