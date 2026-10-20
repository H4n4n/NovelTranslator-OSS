"""Test-only v1 replay harness; frozen PR146 assertions stay byte-for-byte intact.

The historical suite deliberately executes its 24K production-era controls.
Current production and all new tests execute 50K; this harness cannot be imported
by a production executor and never changes any historical evidence on disk.
"""
import copy
import sys
import pytest

# --- Public CI zero-provider guard (migration-specific; synthetic tests only) ---
import os as _os
import re as _re
import subprocess as _subprocess
import sys as _sys
from pathlib import Path as _Path

_GUARD_PYTHONPATH = _os.environ.get("NOVELTRANSLATOR_GUARD_PYTHONPATH")
_GUARD_USER_SITE = _os.environ.get("NOVELTRANSLATOR_GUARD_USER_SITE")
_GUARD_ALLOWED = [_Path(p) for p in _os.environ.get("NOVELTRANSLATOR_GUARD_ALLOWED_ROOTS", "").split(_os.pathsep) if p]
_GUARD_DENIED = [_Path(p) for p in _os.environ.get("NOVELTRANSLATOR_GUARD_DENY_ROOTS", "").split(_os.pathsep) if p]
_GUARD_PRIVATE = [_Path(p) for p in _os.environ.get("NOVELTRANSLATOR_GUARD_PRIVATE_ROOTS", "").split(_os.pathsep) if p]
_ISOLATION_ENV = ("TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PYTHONPYCACHEPREFIX")
_GUARD_ENV = ("NOVELTRANSLATOR_GUARD_ALLOWED_ROOTS", "NOVELTRANSLATOR_GUARD_DENY_ROOTS",
              "NOVELTRANSLATOR_GUARD_PRIVATE_ROOTS", "NOVELTRANSLATOR_GUARD_PYTHONPATH",
              "NOVELTRANSLATOR_GUARD_USER_SITE")
_CLEAR_RE = _re.compile(r"(?i)(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH|DEEPSEEK|OPENAI|ANTHROPIC|PROVIDER|LIVE|API|HISTORICAL|NOVELTRANSLATOR|DOTENV)")


def _under(path, root):
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _zero_provider_guard(event, args):
    if event in {"socket.connect", "socket.getaddrinfo", "socket.gethostbyname", "socket.sendto", "socket.sendmsg"}:
        raise RuntimeError("ZERO Provider suite: network forbidden")
    if event == "ctypes.dlsym" and len(args) > 1 and str(args[1]) in {"CryptProtectData", "CryptUnprotectData"}:
        raise RuntimeError("ZERO Provider suite: real DPAPI forbidden")
    if event == "open" and args and isinstance(args[0], (str, bytes, _os.PathLike)):
        path = _Path(_os.fsdecode(args[0])).resolve()
        if any(_under(path, root.resolve()) for root in _GUARD_ALLOWED):
            return
        if path.suffix.lower() == ".dpapi" or path.name == ".env":
            raise RuntimeError("ZERO Provider suite: real credential file access forbidden")
        if any(_under(path, root.resolve()) for root in _GUARD_DENIED):
            raise RuntimeError("ZERO Provider suite: protected production root forbidden")
        for root in _GUARD_PRIVATE:
            for private in ("novels", "data", "backup", "backups", "incoming", ".commandcode"):
                if (root / private) == path or (root / private) in path.parents:
                    raise RuntimeError("ZERO Provider suite: private project data access forbidden")
        if any(part.startswith("NovelTranslator_") for part in path.parts):
            raise RuntimeError("ZERO Provider suite: private worktree access forbidden")


def _install_zero_provider_guard():
    if _os.environ.get("NOVELTRANSLATOR_GUARD_ACTIVE") == "1":
        return
    _os.environ["NOVELTRANSLATOR_GUARD_ACTIVE"] = "1"
    _sys.addaudithook(_zero_provider_guard)

    original_init = _subprocess.Popen.__init__

    def guarded_init(self, *args, **kwargs):
        env = kwargs.get("env")
        env = dict(env) if env is not None else _os.environ.copy()
        if _GUARD_PYTHONPATH:
            parts = [p for p in env.get("PYTHONPATH", "").split(_os.pathsep) if p]
            if _GUARD_PYTHONPATH not in parts:
                parts.insert(0, _GUARD_PYTHONPATH)
            if _GUARD_USER_SITE and _GUARD_USER_SITE not in parts:
                parts.append(_GUARD_USER_SITE)
            env["PYTHONPATH"] = _os.pathsep.join(parts)
        for key in _ISOLATION_ENV:
            if key in _os.environ:
                env[key] = _os.environ[key]
        for key in _GUARD_ENV:
            if key in _os.environ:
                env[key] = _os.environ[key]
        for key in list(env):
            if key.startswith("NOVELTRANSLATOR_GUARD_"):
                continue
            if _CLEAR_RE.search(key):
                env.pop(key, None)
        env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
        kwargs["env"] = env
        return original_init(self, *args, **kwargs)

    _subprocess.Popen.__init__ = guarded_init


_install_zero_provider_guard()
# --- end public CI guard ---


def install_synthetic_goat_hard_bound(monkeypatch, tmp_path):
    """Disposable, file-backed capability; never real-provider metadata."""
    from dataclasses import replace
    from types import MappingProxyType
    import hashlib
    import json
    import production_goat_auxiliary_policy as policy
    path = tmp_path / "synthetic_context_capability.json"
    capability = replace(policy.COMMAND_CODE_FLASH_CAPABILITY, context_tokens=131072,
        evidence_path=str(path), evidence_sha256="0"*64, revision="synthetic-context-capability-v1",
        primary_source="synthetic-test-only", secondary_source="synthetic-test-only",
        primary_source_reviewed_fact="synthetic context = 131072 tokens",
        secondary_source_reviewed_fact="synthetic fixture, no real capability claimed",
        normalization_explanation="Synthetic test bound only.")
    raw = (json.dumps(capability.expected_evidence(), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    path.write_bytes(raw)
    capability = replace(capability, evidence_sha256=hashlib.sha256(raw).hexdigest())
    monkeypatch.setattr(policy, "REVIEWED_HARD_BOUNDS", MappingProxyType({
        (capability.provider, capability.model): capability}))
    return capability


@pytest.fixture
def synthetic_goat_hard_bound(monkeypatch, tmp_path):
    return install_synthetic_goat_hard_bound(monkeypatch, tmp_path)


@pytest.fixture(autouse=True)
def historical_pending_fixture_generation(request, monkeypatch):
    """The unchanged v1 compatibility test generates exact old 24K/8K shapes.

    It explicitly injects its independent literal v1 provider. Preserve that
    fixture's historical controls/predicate while every active-v3 path uses the
    current production normalizer. This never reinterprets or edits evidence.
    """
    if request.node.path.name != "test_goat_historical_identity.py" or request.node.name != "test_old_pending_manual_review_before_send_or_new_journal":
        # Parametrized node names include the synthetic incident suffix.
        if request.node.path.name != "test_goat_historical_identity.py" or not request.node.name.startswith("test_old_pending_manual_review_before_send_or_new_journal["):
            return
    import production_goat_runtime as runtime
    import production_goat_glossary_review_recovery as review
    from benchmark_longitudinal_g7c_stage1_integration import normalize_legacy_request
    from benchmark_longitudinal_g7j_reasoning_budget import translation_completion_budget, goat_auxiliary_stage1_runtime_completion_budget
    normalizer = runtime.normalize_production_request
    eligible = review.eligible_envelope
    def normalize(logical, kwargs):
        identity = runtime.provider_identity("command_code_provider", "v41-flash")
        if identity["revision"] != "production-provider-runtime-v1":
            return normalizer(logical, kwargs)
        body = {**copy.deepcopy(kwargs), "model": identity["wire_model"]}
        body = normalize_legacy_request(model_id=identity["wire_model"], logical_operation=logical, request_kwargs=body)
        body["max_completion_tokens"] = (translation_completion_budget(logical_operation=logical,
            requested_tokens=body["max_completion_tokens"], reasoning_effort="low") if logical == "translation"
            else goat_auxiliary_stage1_runtime_completion_budget(logical_operation=logical, requested_tokens=body["max_completion_tokens"]))
        return body
    def historical_eligible(envelope, wire, *, original=False):
        identity = review.provider_identity("command_code_provider", "v41-flash")
        if identity["revision"] != "production-provider-runtime-v1":
            return eligible(envelope, wire, original=original)
        stats = envelope.get("stats", {})
        return (original is True and wire.get("model") == identity["wire_model"]
            and wire.get("reasoning_effort") == "low" and wire.get("max_completion_tokens") == 8000
            and envelope.get("provider_identity") == identity and envelope.get("finish_reason") == "stop"
            and isinstance(envelope.get("content"), str) and not envelope["content"].strip()
            and stats.get("completion_tokens", 0) > 0 and stats.get("reasoning_tokens_exposed") is True
            and stats.get("reasoning_tokens") == stats["completion_tokens"] and stats.get("visible_completion_tokens") == 0
            and review.accepted_envelope(envelope, wire) == envelope)
    monkeypatch.setattr(runtime, "normalize_production_request", normalize)
    monkeypatch.setattr(review, "eligible_envelope", historical_eligible)


@pytest.fixture(autouse=True)
def frozen_pr146_runtime(request, monkeypatch):
    if request.node.path.name != "test_goat_saturation_recovery.py":
        return
    request.getfixturevalue("synthetic_goat_hard_bound")
    import production_provider_runtime as provider
    import production_goat_runtime as runtime
    import production_goat_cap_recovery as cap
    import production_goat_saturation_recovery as v1
    import goat_saturation_recovery_plan as planner
    import benchmark_longitudinal_g7j_reasoning_budget as g7j
    from benchmark_longitudinal_g7c_stage1_integration import normalize_legacy_request
    identity = provider.historical_goat_provider_identity()
    original_provider = provider.provider_identity

    def historical_provider(selection, model):
        if selection == "command_code_provider":
            return copy.deepcopy(identity)
        return original_provider(selection, model)

    def historical_request(logical, kwargs):
        body = copy.deepcopy(kwargs)
        if body.get("model") != "deepseek-flash":
            raise runtime.GoatRuntimeError("historical logical model drift")
        body["model"] = identity["wire_model"]
        body = normalize_legacy_request(model_id=identity["wire_model"],
            logical_operation=logical, request_kwargs=body)
        body["max_completion_tokens"] = (
            g7j.translation_completion_budget(logical_operation=logical,
                requested_tokens=body["max_completion_tokens"], reasoning_effort=body["reasoning_effort"])
            if logical == "translation" else
            g7j.goat_auxiliary_stage1_runtime_completion_budget(logical_operation=logical,
                requested_tokens=body["max_completion_tokens"]))
        return body

    replacements = [(original_provider, historical_provider),
                    (runtime.normalize_production_request, historical_request),
                    (runtime.classify_translation_response, g7j.classify_translation_response)]
    for name in ("eligible_envelope", "run_goat_translation_stage", "recovery_binding", "prove_initial",
                 "journal_identity", "accepted_envelope", "policy_sha256"):
        replacements.append((getattr(cap, name), getattr(v1, name)))
    for module in list(sys.modules.values()):
        if module is None or not getattr(module, "__file__", ""):
            continue
        if "goat" not in module.__name__ and "production" not in module.__name__ and "controlled" not in module.__name__ and "operator" not in module.__name__ and "pending" not in module.__name__:
            continue
        for name, value in list(vars(module).items()):
            for old, new in replacements:
                if value is old:
                    monkeypatch.setattr(module, name, new)
        if getattr(module, "IDENTITY", None) == original_provider("command_code_provider", "v41-flash"):
            monkeypatch.setattr(module, "IDENTITY", copy.deepcopy(identity))
    for module in (cap, planner):
        for name in ("KIND", "REVISION", "STRATEGY"):
            monkeypatch.setattr(module, name, getattr(v1, name))
