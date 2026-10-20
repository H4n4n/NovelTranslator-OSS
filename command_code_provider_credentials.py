"""Production Command Code Provider credential seam.

The GOAT inference credential is a DPAPI-protected secret at
``%LOCALAPPDATA%\\NovelTranslator\\Secrets\\command_code_api_key.dpapi``. This
module is the production-side seam that makes it available to a paid executor
for the smallest possible window.

Guarantees enforced here (see tests):

* the plaintext exists only in process memory for the duration of a paid
  operation;
* it is never printed, logged, returned in reports, or persisted;
* the real secret is never read while zero-provider tests run — tests inject a
  fake DPAPI backend and a fake secret path.

The DPAPI read/validate machinery is reused from the reviewed G7G secure
launcher rather than duplicated.
"""
import os
from contextlib import contextmanager

from benchmark_longitudinal_g7g_stage1_secure_launcher import (
    _read_secret_key as _g7g_read_secret_key,
    _sanitize_text as _g7g_sanitize_text,
    default_secret_path,
    load_platform_dpapi,
)

COMMAND_CODE_API_KEY_ENV = "COMMAND_CODE_API_KEY"


class CommandCodeCredentialError(RuntimeError):
    """The Command Code Provider credential could not be made available."""


def command_code_secret_path(*, env=None):
    """Return the default DPAPI secret path without reading it."""
    return default_secret_path(env)


def read_command_code_api_key(secret_path, *, dpapi):
    """Return the plaintext key from a DPAPI secret; never logs anything."""
    try:
        return _g7g_read_secret_key(secret_path, dpapi)
    except Exception as exc:
        # Never surface ciphertext or a partial secret in the message.
        raise CommandCodeCredentialError(
            "could not read the Command Code Provider DPAPI secret"
        ) from exc


def redact_secret(value, secret):
    """Replace a secret with '[REDACTED]' anywhere it appears in a report."""
    return _g7g_sanitize_text(value, [secret])


@contextmanager
def command_code_api_key_environment(*, secret_path=None, dpapi=None, env=None):
    """Expose the Command Code Provider key for one paid operation.

    Mirrors the direct-DeepSeek credential bridge: the previous environment
    state is restored in ``finally`` so this seam cannot permanently alter the
    caller, and the plaintext is dropped from this frame's reference afterward.
    """
    previous_present = COMMAND_CODE_API_KEY_ENV in os.environ
    previous_value = os.environ.get(COMMAND_CODE_API_KEY_ENV)
    key = None
    try:
        path = secret_path if secret_path is not None else command_code_secret_path(env=env)
        backend = dpapi if dpapi is not None else load_platform_dpapi()
        key = read_command_code_api_key(path, dpapi=backend)
        os.environ[COMMAND_CODE_API_KEY_ENV] = key
        yield {"source": "command_code_dpapi", "secret_path": str(path)}
    finally:
        if previous_present:
            os.environ[COMMAND_CODE_API_KEY_ENV] = previous_value or ""
        else:
            os.environ.pop(COMMAND_CODE_API_KEY_ENV, None)
        key = None


@contextmanager
def command_code_api_key_scope(*, secret_path=None, dpapi=None, env=None):
    """Memory-only key scope, entered only after paid execution authorization.

    No environment bridge. Client owners must close/drop their client before
    exiting. Python strings cannot promise physical memory zeroization.
    """
    key = None
    try:
        path = secret_path if secret_path is not None else command_code_secret_path(env=env)
        backend = dpapi if dpapi is not None else load_platform_dpapi()
        key = read_command_code_api_key(path, dpapi=backend)
        yield key
    except Exception:
        # Neither plaintext nor provider exception bodies escape this scope.
        raise CommandCodeCredentialError("Command Code Provider credential/execution scope failed") from None
    finally:
        key = None
