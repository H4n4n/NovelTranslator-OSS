#!/usr/bin/env python3
"""Guarded ZERO-provider pytest runner.

Usage:
    python tests/zero_provider_validation.py --basetemp <external-temp-child> \
        --deselect <node-id> [--deselect <node-id> ...] -- <pytest args...>

Requirements enforced:
- explicit --basetemp under the system temp root and outside the checkout;
- explicit deselection of the real-Windows-DPAPI round-trip node;
- provider/live/credential environment controls cleared;
- stable guard/home/temp/cache state OUTSIDE the pytest-managed basetemp;
- network/DPAPI/private-root audit guards in this process and descendants;
- explicit denial of protected production/GOAT roots.

Python audit hooks are defense-in-depth regression protection, not an operating
system sandbox against arbitrary native code.
"""
from __future__ import annotations

import os
import re
import site
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))
_USER_SITE = site.getusersitepackages()
DPAPI_NODE = "tests/test_benchmark_longitudinal_g7g_stage1_secure_launcher.py::test_real_windows_dpapi_round_trip"
DEFAULT_DENY_ROOTS = [r"D:\NovelTranslator", r"D:\NovelTranslator_GOATBenchmark"]

CLEAR_ENV_RE = re.compile(
    r"(?i)(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH|DEEPSEEK|OPENAI|ANTHROPIC|PROVIDER|LIVE|"
    r"API|HISTORICAL|NOVELTRANSLATOR|DOTENV)"
)
NETWORK_EVENTS = {"socket.connect", "socket.getaddrinfo", "socket.gethostbyname", "socket.sendto", "socket.sendmsg"}

GUARD_SITECUSTOMIZE = '''"""Auto-imported guard for child Python processes (synthetic tests only)."""
import os
import sys
from pathlib import Path

_ALLOWED = [Path(p) for p in os.environ.get("NOVELTRANSLATOR_GUARD_ALLOWED_ROOTS", "").split(os.pathsep) if p]
_DENIED = [Path(p) for p in os.environ.get("NOVELTRANSLATOR_GUARD_DENY_ROOTS", "").split(os.pathsep) if p]
_PRIVATE_ROOTS = [Path(p) for p in os.environ.get("NOVELTRANSLATOR_GUARD_PRIVATE_ROOTS", "").split(os.pathsep) if p]


def _under(path, root):
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _normalized(path):
    return str(path).replace("/", "\\\\").casefold()


def _guard(event, args):
    if event in {"socket.connect", "socket.getaddrinfo", "socket.gethostbyname", "socket.sendto", "socket.sendmsg"}:
        raise RuntimeError("ZERO Provider suite: network forbidden")
    if event == "ctypes.dlsym" and len(args) > 1 and str(args[1]) in {"CryptProtectData", "CryptUnprotectData"}:
        raise RuntimeError("ZERO Provider suite: real DPAPI forbidden")
    if event == "open" and args and isinstance(args[0], (str, bytes, os.PathLike)):
        path = Path(os.fsdecode(args[0])).resolve()
        if any(_under(path, root.resolve()) for root in _ALLOWED):
            return
        if path.suffix.lower() == ".dpapi" or path.name == ".env":
            raise RuntimeError("ZERO Provider suite: real credential file access forbidden")
        if any(_under(path, root.resolve()) for root in _DENIED):
            raise RuntimeError("ZERO Provider suite: protected production root forbidden")
        for root in _PRIVATE_ROOTS:
            if _under(path, root / "novels") or _under(path, root / "data") or _under(path, root / "backup"):
                raise RuntimeError("ZERO Provider suite: private project data access forbidden")
        if any(part.startswith("NovelTranslator_") for part in path.parts):
            raise RuntimeError("ZERO Provider suite: private worktree access forbidden")


sys.addaudithook(_guard)
'''


def _parse(argv):
    basetemp = None
    deselect = []
    rest = []
    index = 0
    passthrough = False
    while index < len(argv):
        item = argv[index]
        if passthrough:
            rest.append(item)
        elif item == "--":
            passthrough = True
        elif item.startswith("--basetemp="):
            basetemp = item.split("=", 1)[1]
        elif item == "--basetemp":
            index += 1
            basetemp = argv[index]
        elif item.startswith("--deselect="):
            deselect.append(item.split("=", 1)[1])
        elif item == "--deselect":
            index += 1
            deselect.append(argv[index])
        else:
            rest.append(item)
        index += 1
    return basetemp, deselect, rest


def _validate_basetemp(value):
    if not value:
        raise RuntimeError("explicit external --basetemp is required")
    temp = Path(value).resolve()
    system_temp = Path(tempfile.gettempdir()).resolve()
    if temp == system_temp or system_temp not in temp.parents:
        raise RuntimeError("test temporary root must be a child of the system temporary directory")
    if temp == REPO or REPO in temp.parents or temp in REPO.parents:
        raise RuntimeError("test temporary root must be external to the code checkout")
    return temp


def _clear_environment():
    removed = []
    for key in list(os.environ):
        if CLEAR_ENV_RE.search(key):
            removed.append(key)
            os.environ.pop(key, None)
    return removed


def _deny_roots():
    configured = os.environ.get("NOVELTRANSLATOR_GUARD_DENY_ROOTS")
    if configured:
        return [part for part in configured.split(os.pathsep) if part]
    return list(DEFAULT_DENY_ROOTS)


def _guard(event, args):
    if event in NETWORK_EVENTS:
        raise RuntimeError("ZERO Provider suite: network forbidden")
    if event == "ctypes.dlsym" and len(args) > 1 and str(args[1]) in {"CryptProtectData", "CryptUnprotectData"}:
        raise RuntimeError("ZERO Provider suite: real DPAPI forbidden")
    if event == "open" and args and isinstance(args[0], (str, bytes, os.PathLike)):
        path = Path(os.fsdecode(args[0])).resolve()
        if any(root == path or root in path.parents for root in _ALLOWED_ROOTS):
            return
        if path.suffix.lower() == ".dpapi" or path.name == ".env":
            raise RuntimeError("ZERO Provider suite: real credential file access forbidden")
        if any(root == path or root in path.parents for root in _DENY_ROOTS):
            raise RuntimeError("ZERO Provider suite: protected production root forbidden")
        for root in (REPO,):
            for private in ("novels", "data", "backup", "backups", "incoming", ".commandcode"):
                if (root / private) == path or (root / private) in path.parents:
                    raise RuntimeError("ZERO Provider suite: private project data access forbidden")
        if any(part.startswith("NovelTranslator_") for part in path.parts):
            raise RuntimeError("ZERO Provider suite: private worktree access forbidden")


_ALLOWED_ROOTS = []
_DENY_ROOTS = []


def main(argv):
    global _ALLOWED_ROOTS, _DENY_ROOTS
    basetemp, deselect, pytest_args = _parse(argv)
    temp = _validate_basetemp(basetemp)
    if DPAPI_NODE not in deselect:
        raise RuntimeError("explicit real-DPAPI node deselection is required")
    if (REPO / ".env").exists():
        raise RuntimeError("credential-free checkout required; .env is present")

    _clear_environment()
    state_root = temp.parent / (temp.name + "_state")
    for name in ("tmp", "home", "appdata", "localappdata", "pycache", "guard"):
        (state_root / name).mkdir(parents=True, exist_ok=True)
    os.environ.update({
        "TEMP": str(state_root / "tmp"),
        "TMP": str(state_root / "tmp"),
        "TMPDIR": str(state_root / "tmp"),
        "HOME": str(state_root / "home"),
        "USERPROFILE": str(state_root / "home"),
        "APPDATA": str(state_root / "appdata"),
        "LOCALAPPDATA": str(state_root / "localappdata"),
        "PYTHONPYCACHEPREFIX": str(state_root / "pycache"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHON_DOTENV_DISABLED": "1",
        "NOVELTRANSLATOR_GUARD_ALLOWED_ROOTS": os.pathsep.join([str(temp), str(state_root)]),
        "NOVELTRANSLATOR_GUARD_PRIVATE_ROOTS": str(REPO),
        "NOVELTRANSLATOR_GUARD_DENY_ROOTS": os.pathsep.join(_deny_roots()),
    })
    guard_dir = state_root / "guard"
    (guard_dir / "sitecustomize.py").write_text(GUARD_SITECUSTOMIZE, encoding="utf-8")
    existing = os.environ.get("PYTHONPATH", "")
    guard_path = str(guard_dir)
    parts = [guard_path, str(REPO), str(REPO / "tests")]
    if _USER_SITE and Path(_USER_SITE).is_dir():
        parts.append(_USER_SITE)
    for part in existing.split(os.pathsep):
        if part and part not in parts:
            parts.append(part)
    os.environ["PYTHONPATH"] = os.pathsep.join(parts)
    os.environ["NOVELTRANSLATOR_GUARD_PYTHONPATH"] = guard_path
    os.environ["NOVELTRANSLATOR_GUARD_USER_SITE"] = _USER_SITE if _USER_SITE and Path(_USER_SITE).is_dir() else ""

    _ALLOWED_ROOTS = [temp, state_root]
    _DENY_ROOTS = [Path(part).resolve() for part in _deny_roots()]
    sys.addaudithook(_guard)
    os.chdir(REPO)

    try:
        import command_code_provider_credentials as credentials

        def forbidden_dpapi(*args, **kwargs):
            raise RuntimeError("ZERO Provider suite: real DPAPI forbidden")

        credentials.load_platform_dpapi = forbidden_dpapi
    except Exception:
        pass

    import pytest

    final_args = list(pytest_args) + ["--basetemp=" + str(temp)]
    for node in deselect:
        final_args += ["--deselect", node]
    return pytest.main(final_args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
