"""Zero-provider focused tests for the G11 practical replacement benchmark.

Every provider interaction goes through an injected fake client, and the
production lifecycle is a scripted fake module injected through the runner's
``production_module_loader`` seam.  The scripted lifecycle still goes through
``install_g11_paid_runtime``, the durable paid-request journal, the retry
wrapper, the durable G11 ledger, and accepted-usage settlement exactly like the
real engine.  No test reads a credential, constructs a real client, runs a real
production chapter, or mutates a frozen/external root.
"""

import builtins
import copy
import hashlib
import importlib.util
import json
import os
import shutil
import socket
import sys
import tempfile
import uuid
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

import benchmark_longitudinal_g11_preregistration as prereg
import benchmark_longitudinal_g11_runner as runner
import production_paid_request_journal as journal_mod
from benchmark_longitudinal_g10_safety import G10SafetyError
from benchmark_longitudinal_g7c_stage1_integration import CONTEXT_ENTRYPOINTS
from noveltranslator_core import atomic_write_json


REPO_ROOT = Path(__file__).resolve().parents[1]
API_KEY = "unit-test-injected-key"
GLM_ACE = "g11-run01-glm-ace_evolution"
DEEPSEEK_ACE = "g11-run02-deepseek-ace_evolution"
GLM_ROGUE = "g11-run03-glm-rogue_first_prince"
TRANSLATION_TOKENS = 24000
AUX_TOKENS = 8000
CREDENTIAL_ENV_NAMES = frozenset(
    {
        "DEEPSEEK_API_KEY",
        "OPENAI_API_KEY",
        "NOVELTRANSLATOR_TRANSLATION_MODEL",
        "ANTHROPIC_API_KEY",
        "COMMAND_CODE_API_KEY",
    }
)


# ---------------------------------------------------------------------------
# Synthetic pristine S0 template and fake production root
# ---------------------------------------------------------------------------


def _chapter_library(final_chapter):
    return [
        {
            "chapter": index,
            "production_index": index,
            "volume_index": 1,
            "local_chapter_number": index,
            "local_chapter_start": index,
            "local_chapter_end": index,
            "heading": f"Source heading {index}",
            "text": f"source text for chapter {index}",
        }
        for index in range(1, final_chapter + 1)
    ]


def _write_json(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _make_pristine(root):
    """Build a synthetic pristine S0 template with both G11 novels."""
    root.mkdir(parents=True, exist_ok=True)
    for novel_id, spec in prereg.NOVELS.items():
        project = root / "runs" / f"g10b-pristine-{novel_id}"
        (project / "data").mkdir(parents=True, exist_ok=True)
        (project / "source").mkdir(parents=True, exist_ok=True)
        config = {
            "id": novel_id,
            "title": spec["title"],
            "source_language": spec["source_language"],
            "target_language": spec["target_language"],
            "profile": spec["profile"],
            "final_chapter": spec["final_chapter"],
            "audit_interval": spec["audit_interval"],
            "chapter_numbering": spec["chapter_numbering"],
        }
        if spec["source_type"] != "epub":
            config["source_type"] = spec["source_type"]
        _write_json(project / "novel.json", config)
        _write_json(
            project / "data" / "chapters.json",
            _chapter_library(spec["final_chapter"]),
        )
        (project / "translation_addendum.txt").write_text(
            "", encoding="utf-8", newline="\n"
        )
        suffix = "txt" if spec["source_type"] == "txt" else "epub"
        (project / "source" / f"novel.{suffix}").write_bytes(
            f"{novel_id}-source-bytes".encode("utf-8")
        )
    return root


def _make_production_state(root):
    """Build a fake live production root that G11 must never touch."""
    root.mkdir(parents=True, exist_ok=True)
    for novel_id in prereg.NOVELS:
        project = root / "novels" / novel_id
        (project / "data").mkdir(parents=True, exist_ok=True)
        (project / "output" / "chapters").mkdir(parents=True, exist_ok=True)
        _write_json(project / "novel.json", {"id": novel_id})
        _write_json(project / "data" / "progress.json", {"last_completed": 7})
        (project / "output" / "chapters" / "0007.md").write_text(
            "live production translation\n", encoding="utf-8", newline="\n"
        )
    return root


def _tree_snapshot(root):
    root = Path(root)
    return sorted(
        (path.relative_to(root).as_posix(), path.read_bytes())
        for path in root.rglob("*")
        if path.is_file()
    )


# ---------------------------------------------------------------------------
# Fake provider transport
# ---------------------------------------------------------------------------


class _TransportError(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def _response(kwargs, *, content="scripted content", finish_reason="stop",
              prompt=120, completion=30, cached=0):
    return SimpleNamespace(
        id="fake-response-id",
        model=kwargs["model"],
        choices=[
            SimpleNamespace(
                finish_reason=finish_reason,
                message=SimpleNamespace(content=content),
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=prompt + completion,
            prompt_tokens_details=SimpleNamespace(cached_tokens=cached),
        ),
    )


class _FakeCompletions:
    def __init__(self, responder):
        self.calls = []
        self._responder = responder

    def create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        return self._responder(kwargs)


class _FakeFactory:
    def __init__(self, completions):
        self.completions = completions
        self.kwargs = []

    def __call__(self, **kwargs):
        self.kwargs.append(copy.deepcopy(kwargs))
        return SimpleNamespace(
            chat=SimpleNamespace(completions=self.completions)
        )


# ---------------------------------------------------------------------------
# Scripted real-engine stand-in
# ---------------------------------------------------------------------------


def _legacy_kwargs(module, content, *, max_tokens):
    return {
        "model": module.MODEL,
        "messages": [
            {"role": "system", "content": "system::g11-scripted-lifecycle"},
            {"role": "user", "content": content},
        ],
        "max_tokens": max_tokens,
        "extra_body": {"thinking": {"type": "disabled"}},
    }


def _fake_module(track, run_root, *, audit, seen, fail_chapters=(), qa_block_attempts=()):
    """Build one scripted lifecycle module with the real patched vocabulary."""
    module = SimpleNamespace(
        _RUNTIME_INITIALIZED=True,
        client=None,
        api_key=None,
        MODEL=None,
        AUTO_OFF_PEAK_ONLY=True,
        journaled_chat_request=lambda *a, **k: None,
        call_api_with_retries=lambda **k: None,
        send_api_request_once=lambda **k: None,
        usage_and_cost=lambda usage: {},
        usage_and_cost_for_pricing=lambda usage, pricing: {},
        reprice_saved_stats_if_needed=lambda stats: stats,
        pricing_snapshot=lambda moment=None: {},
        ACTIVE_NOVEL_CONFIG=SimpleNamespace(id=track["novel_id"]),
        ACTIVE_PROFILE=SimpleNamespace(id=track["profile_id"]),
        PAID_REQUEST_JOURNAL_DIR=Path(run_root) / "data" / "provider_request_journal",
    )
    for name in CONTEXT_ENTRYPOINTS:
        setattr(module, name, lambda *a, **k: None)
    qa_blocks = {int(chapter): int(count) for chapter, count in qa_block_attempts}

    def run_periodic_audit(start_chapter, end_chapter):
        response = module.call_api_with_retries(
            **_legacy_kwargs(
                module,
                f"audit chunk for {start_chapter}-{end_chapter}",
                max_tokens=4000,
            )
        )
        module.usage_and_cost(response.usage)
        seen.setdefault("audits", []).append((start_chapter, end_chapter))

    def run_unattended_production(*, target_chapter=None, **_kwargs):
        chapter = int(target_chapter)
        seen.setdefault("chapters", []).append(chapter)
        translation = module.journaled_chat_request(
            "chapter_translation",
            chapter,
            f"source text for chapter {chapter}",
            _legacy_kwargs(
                module,
                f"{track['novel_id']} translate chapter {chapter}",
                max_tokens=12000,
            ),
        )
        seen.setdefault("translations", {})[chapter] = translation["response"]["content"]
        if chapter in fail_chapters:
            raise RuntimeError(f"scripted engine failure at chapter {chapter}")
        if qa_blocks.get(chapter, 0) > 0:
            qa_blocks[chapter] -= 1
            pending = Path(run_root) / "output" / "pending"
            pending.mkdir(parents=True, exist_ok=True)
            (pending / f"{chapter:04d}.md").write_text(
                f"rejected pending chapter {chapter}\n", encoding="utf-8", newline="\n"
            )
            _write_json(
                pending / f"{chapter:04d}.json",
                {
                    "chapter": chapter,
                    "stage": "translated",
                    "local_qa": {"commit_allowed": False},
                },
            )
            raise SystemExit(1)
        module.journaled_chat_request(
            "state_extraction",
            chapter,
            f"source text for chapter {chapter}",
            _legacy_kwargs(
                module,
                f"{track['novel_id']} extract state for chapter {chapter}",
                max_tokens=3000,
            ),
            translation_text=f"translation for chapter {chapter}",
        )
        progress_path = Path(run_root) / "data" / "progress.json"
        state = json.loads(progress_path.read_text(encoding="utf-8"))
        state["last_completed"] = chapter
        state["next_chapter"] = chapter + 1
        if audit and chapter in prereg.AUDIT_CHECKPOINTS:
            state["last_audited_chapter"] = chapter
        _write_json(progress_path, state)
        (Path(run_root) / "output" / "chapters").mkdir(parents=True, exist_ok=True)
        (Path(run_root) / "output" / "chapters" / f"{chapter:04d}.md").write_text(
            f"translated chapter {chapter}\n", encoding="utf-8", newline="\n"
        )
        (Path(run_root) / "output" / "qa").mkdir(parents=True, exist_ok=True)
        _write_json(
            Path(run_root) / "output" / "qa" / f"{chapter:04d}.json",
            {"chapter": chapter, "qa": "scripted"},
        )
        # The real engine runs its own due audit inside the same invocation.
        if audit and chapter in prereg.AUDIT_CHECKPOINTS:
            module.run_periodic_audit(max(1, chapter - 24), chapter)

    module.run_periodic_audit = run_periodic_audit
    module.run_unattended_production = run_unattended_production
    return module


# ---------------------------------------------------------------------------
# Fixtures and drivers
# ---------------------------------------------------------------------------


@pytest.fixture
def work():
    """A private scratch directory, independent of pytest's tmp_path base.

    The default pytest temp base can be unavailable in hardened Windows
    environments, so G11 tests create their own isolated scratch root.
    """
    base = Path(tempfile.mkdtemp(prefix="g11-tests-"))
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


@pytest.fixture
def env(work):
    pristine = _make_pristine(work / "s0_template")
    production = _make_production_state(work / "production")
    execution = work / "g11_execution"
    result = runner.prepare_g11(
        execution_root=execution,
        pristine_root=pristine,
        production_root=production,
    )
    return {
        "tmp_path": work,
        "pristine": pristine,
        "production": production,
        "execution": execution,
        "prepare_result": result,
    }


def _execute(env, run_id, *, responder, max_chapters=1, audit=True,
             fail_chapters=(), qa_block_attempts=()):
    completions = _FakeCompletions(responder)
    factory = _FakeFactory(completions)
    seen = {}
    delays = []

    def loader(*, code_root, track_root_path):
        module = _fake_module(
            prereg.TRACK_BY_ID[run_id],
            Path(track_root_path),
            audit=audit,
            seen=seen,
            fail_chapters=fail_chapters,
            qa_block_attempts=qa_block_attempts,
        )
        seen["module"] = module
        return {"module": module}

    report = runner.run_g11_track(
        execution_root=env["execution"],
        run_id=run_id,
        api_key=API_KEY,
        client_factory=factory,
        production_module_loader=loader,
        max_chapters=max_chapters,
        sleep=lambda seconds: delays.append(seconds),
    )
    return report, completions, factory, seen, delays


def _ledger(env, model_id):
    return runner.G11Ledger.open(
        path=runner.ledger_path(env["execution"], model_id), model_id=model_id
    )


def _track_root(env, run_id):
    return runner.track_root(env["execution"], run_id)


def _journal_kinds(env, run_id):
    directory = _track_root(env, run_id) / "data" / "provider_request_journal"
    if not directory.is_dir():
        return []
    return sorted(path.name.split(".")[1] for path in directory.glob("*.json"))


def _reservations(ledger, operation):
    return [
        record
        for record in ledger.document["reservations"]
        if record["operation"] == operation
    ]


def _attempts_for(ledger, operation):
    reservation_ids = {
        record["reservation_id"] for record in _reservations(ledger, operation)
    }
    return [
        attempt
        for attempt in ledger.document["attempts"]
        if attempt["reservation_id"] in reservation_ids
    ]


def _seed_progress(env, run_id, *, last_completed, last_audited_chapter=0):
    root = _track_root(env, run_id)
    state = json.loads((root / "data" / "progress.json").read_text(encoding="utf-8"))
    state["last_completed"] = last_completed
    state["next_chapter"] = last_completed + 1
    state["completed"] = list(range(1, last_completed + 1))
    state["last_audited_chapter"] = last_audited_chapter
    _write_json(root / "data" / "progress.json", state)


def _seed_pending(env, run_id, chapter, *, text="rejected pending body\n"):
    pending = _track_root(env, run_id) / "output" / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    markdown = pending / f"{chapter:04d}.md"
    markdown.write_text(text, encoding="utf-8", newline="\n")
    _write_json(
        pending / f"{chapter:04d}.json",
        {"chapter": chapter, "stage": "translated", "local_qa": {"commit_allowed": False}},
    )
    return [markdown, pending / f"{chapter:04d}.json"]


def _seed_translation_journal(
    env, run_id, chapter, *, operation_kind="g11_translation",
    content="translated", finish_reason="stop",
):
    track = prereg.TRACK_BY_ID[run_id]
    model = prereg.PROVIDER_MODEL_IDS[track["model_id"]]
    directory = _track_root(env, run_id) / "data" / "provider_request_journal"
    directory.mkdir(parents=True, exist_ok=True)
    path = journal_mod.paid_request_journal_path(directory, operation_kind, chapter)
    identity = journal_mod.request_identity(
        operation_kind=operation_kind,
        novel_id=track["novel_id"],
        chapter=chapter,
        provider="command_code_provider",
        model=model,
        profile_id=track["profile_id"],
        source_sha256=hashlib.sha256(f"seed-source-{chapter}".encode()).hexdigest(),
        request_payload={
            "model": model,
            "messages": [{"role": "user", "content": f"seed {chapter}"}],
        },
    )
    journal_mod.run_journaled_paid_request(
        journal_path=path,
        identity=identity,
        save_json=atomic_write_json,
        prepare_attempt=lambda: {"mode": "seed"},
        send_once=lambda: SimpleNamespace(),
        normalize_response=lambda _raw, _ctx: {
            "content": content,
            "finish_reason": finish_reason,
            "stats": {"prompt_tokens": 1, "completion_tokens": 1},
        },
    )
    return path


def _seed_uncertain_journal(env, run_id, chapter, *, operation_kind="g11_translation"):
    track = prereg.TRACK_BY_ID[run_id]
    model = prereg.PROVIDER_MODEL_IDS[track["model_id"]]
    directory = _track_root(env, run_id) / "data" / "provider_request_journal"
    directory.mkdir(parents=True, exist_ok=True)
    path = journal_mod.paid_request_journal_path(directory, operation_kind, chapter)
    identity = journal_mod.request_identity(
        operation_kind=operation_kind,
        novel_id=track["novel_id"],
        chapter=chapter,
        provider="command_code_provider",
        model=model,
        profile_id=track["profile_id"],
        source_sha256=hashlib.sha256(f"uncertain-source-{chapter}".encode()).hexdigest(),
        request_payload={
            "model": model,
            "messages": [{"role": "user", "content": f"uncertain {chapter}"}],
        },
    )

    def boom() -> None:
        raise RuntimeError("scripted ambiguous remote outcome")

    with pytest.raises(RuntimeError):
        journal_mod.run_journaled_paid_request(
            journal_path=path,
            identity=identity,
            save_json=atomic_write_json,
            prepare_attempt=lambda: {"mode": "seed"},
            send_once=boom,
            normalize_response=lambda _raw, _ctx: {},
        )
    assert journal_mod.load_matching_journal(path, identity)["status"] == "uncertain"
    return path


def _seed_quarantine(env, run_id, *, first_blocked_chapter, reason="SystemExit: 1",
                     outcome="terminal_failure"):
    track = prereg.TRACK_BY_ID[run_id]
    return runner.write_quarantine(
        track_root_path=_track_root(env, run_id),
        run_id=run_id,
        model_id=track["model_id"],
        novel_id=track["novel_id"],
        first_blocked_chapter=first_blocked_chapter,
        outcome=outcome,
        reason=reason,
    )


def _translation_calls_for_chapter(completions, chapter):
    token = f"translate chapter {chapter}"
    return [
        call
        for call in completions.calls
        if call.get("max_completion_tokens") == TRANSLATION_TOKENS
        and any(token in message.get("content", "") for message in call.get("messages", []))
    ]


# ---------------------------------------------------------------------------
# 1. Import safety and the frozen preregistration
# ---------------------------------------------------------------------------


def _forbid(*_args, **_kwargs):
    raise AssertionError("G11 import must not perform this operation")


@pytest.mark.parametrize(
    "module_name",
    [
        "benchmark_longitudinal_g11_preregistration",
        "benchmark_longitudinal_g11_runner",
    ],
)
def test_g11_module_import_is_zero_provider_zero_credential(
    monkeypatch, work, module_name
):
    source = REPO_ROOT / f"{module_name}.py"
    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name.split(".")[0] in {"openai", "dotenv"}:
            raise AssertionError(
                "G11 modules must not import the provider SDK or dotenv at import time"
            )
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(socket, "socket", _forbid)
    monkeypatch.setattr(socket, "create_connection", _forbid)
    monkeypatch.setattr(socket, "getaddrinfo", _forbid)
    monkeypatch.setattr(os, "getenv", _forbid)
    monkeypatch.chdir(work)

    unique = f"_g11_import_safety_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(unique, source)
    module = importlib.util.module_from_spec(spec)
    sys.modules[unique] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(unique, None)

    assert [path.name for path in work.rglob("*")] == []
    assert module.__file__


def test_preregistration_is_zero_provider_and_frozen():
    identity = prereg.g11_preregistration_identity()
    assert identity["provider_calls"] == 0
    assert identity["provider_calls_authorized"] is False
    assert identity["credential_access"] is False
    assert prereg.G11_PREREGISTRATION_SHA256 == prereg._sha256_json(identity)
    prereg.assert_zero_provider_design()
    assert prereg.assert_source_bindings(REPO_ROOT)["verified"] is True


# ---------------------------------------------------------------------------
# 2. Preparation: zero provider access, isolation, exact layout
# ---------------------------------------------------------------------------


def test_prepare_g11_makes_zero_provider_calls(env, monkeypatch):
    calls = []

    def forbidden_client(**kwargs):  # pragma: no cover - must not be reached
        calls.append(kwargs)
        raise AssertionError("preparation must not construct a provider client")

    monkeypatch.setattr(runner, "create_provider_client", forbidden_client)
    second_pristine = _make_pristine(env["tmp_path"] / "s0_template_2")
    second_execution = env["tmp_path"] / "g11_execution_2"
    result = runner.prepare_g11(
        execution_root=second_execution,
        pristine_root=second_pristine,
    )
    assert calls == []
    assert result["provider_calls"] == 0
    assert result["frozen_layout_verified"] is True
    manifest = json.loads(
        (second_execution / runner.G11_EXECUTION_MANIFEST_FILENAME).read_text(
            encoding="utf-8"
        )
    )
    assert manifest["provider_calls"] == 0
    assert manifest["credential_access"] is False


def test_prepare_creates_exactly_four_tracks_with_exact_scope(env):
    manifest = json.loads(
        (env["execution"] / runner.G11_EXECUTION_MANIFEST_FILENAME).read_text(
            encoding="utf-8"
        )
    )
    assert len(prereg.TRACKS) == 4
    assert len(prereg.TRACK_BY_ID) == 4
    assert manifest["track_count"] == 4
    assert manifest["chapters_per_track"] == 100
    assert manifest["first_chapter"] == 1
    assert manifest["last_chapter"] == 100
    assert prereg.MAXIMUM_CANONICAL_CHAPTERS == 400
    assert {
        (track["model_id"], track["novel_id"]) for track in prereg.TRACKS
    } == {
        (model_id, novel_id)
        for model_id in prereg.MODEL_ORDER
        for novel_id in prereg.NOVELS
    }
    assert [track["run_index"] for track in prereg.TRACKS] == [1, 2, 3, 4]
    for record in manifest["runs"]:
        identity = json.loads(
            (
                env["execution"]
                / "runs"
                / record["run_id"]
                / runner.G11_RUN_IDENTITY_FILENAME
            ).read_text(encoding="utf-8")
        )
        assert identity["first_chapter"] == 1
        assert identity["last_chapter"] == 100
        assert identity["audit_checkpoints"] == [25, 50, 75, 100]
        assert identity["quality_checkpoints"] == list(prereg.QUALITY_CHECKPOINTS)
        assert identity["provider_calls"] == 0
        assert identity["state"] == "s0_unexecuted"


def test_audit_and_quality_checkpoints_are_exact():
    assert prereg.AUDIT_INTERVAL == 25
    assert prereg.AUDIT_CHECKPOINTS == (25, 50, 75, 100)
    assert prereg.STATE_EVIDENCE_CHECKPOINTS == (25, 50, 75, 100)
    assert prereg.QUALITY_CHECKPOINTS == (
        1, 5, 10, 20, 25, 26, 40, 50, 51, 65, 75, 76, 90, 100,
    )


def test_residual_source_repair_enabled_in_every_experiment_copy(env):
    for track in prereg.TRACKS:
        config = json.loads(
            (_track_root(env, track["run_id"]) / "novel.json").read_text(
                encoding="utf-8"
            )
        )
        repair = config["residual_cjk_micro_repair"]
        assert repair["enabled"] is True
        assert repair["max_total_cjk_chars"] == prereg.RESIDUAL_CJK_MICRO_REPAIR[
            "max_total_cjk_chars"
        ]
        assert repair["max_fragments"] == prereg.RESIDUAL_CJK_MICRO_REPAIR[
            "max_fragments"
        ]
        assert repair["max_attempts_per_pending_version"] == 1


# ---------------------------------------------------------------------------
# 3. Symmetric model policy: effort, budgets, ceilings, raw SDK retries
# ---------------------------------------------------------------------------


def test_runner_targets_the_real_production_engine_and_lifecycle_entrypoint():
    assert runner.G11_PRODUCTION_SCRIPT_NAME == (
        "translate_production_unattended_scheduled_retroactive_dynamic_pricing.py"
    )
    assert (REPO_ROOT / runner.G11_PRODUCTION_SCRIPT_NAME).is_file()
    assert runner.G11_LIFECYCLE_ENTRYPOINT == "run_unattended_production"
    source = (REPO_ROOT / "benchmark_longitudinal_g11_runner.py").read_text(
        encoding="utf-8"
    )
    # The real bounded production entry point and the provider-suppressed
    # experiment loader are the only engine integration.
    assert "load_experiment_engine_module" in source
    assert "initialize_engine_without_provider" in source
    assert "runner(target_chapter=target)" in source


def test_track_requires_the_real_bounded_lifecycle_entrypoint(env):
    responder = lambda kwargs: _response(kwargs, content="ok")
    completions = _FakeCompletions(responder)
    factory = _FakeFactory(completions)
    seen = {}

    def loader(*, code_root, track_root_path):
        module = _fake_module(
            prereg.TRACK_BY_ID[GLM_ACE], Path(track_root_path), audit=False, seen=seen
        )
        del module.run_unattended_production
        return {"module": module}

    report = runner.run_g11_track(
        execution_root=env["execution"],
        run_id=GLM_ACE,
        api_key=API_KEY,
        client_factory=factory,
        production_module_loader=loader,
        max_chapters=1,
        sleep=lambda _seconds: None,
    )
    assert report["status"] == "TERMINAL_FAILURE"
    assert runner.G11_LIFECYCLE_ENTRYPOINT in report["error"]["message"]


def test_effort_policy_is_symmetric_high_translation_semantic_low_structured():
    for model_id in prereg.MODEL_ORDER:
        policy = prereg.MODEL_EFFORT_POLICY[model_id]
        assert policy["translation"] == "high"
        for operation in prereg.SEMANTIC_OPERATIONS:
            assert policy[operation] == "high"
        for operation in prereg.STRUCTURED_OPERATIONS:
            assert policy[operation] == "low"
    assert set(prereg.SEMANTIC_OPERATIONS) == {
        "glossary_review",
        "periodic_audit",
        "terminology_repair",
    }
    assert set(prereg.STRUCTURED_OPERATIONS) == {
        "state_extraction",
        "state_json_repair",
    }


def test_budgets_are_glm_40_and_deepseek_60(work):
    assert prereg.MODEL_BUDGETS[prereg.GLM_MODEL_ID] == "40.00"
    assert prereg.MODEL_BUDGETS[prereg.DEEPSEEK_MODEL_ID] == "60.00"
    glm = runner.G11Ledger.create(
        path=work / "glm.json",
        model_id=prereg.GLM_MODEL_ID,
        ceiling=prereg.MODEL_BUDGETS[prereg.GLM_MODEL_ID],
    )
    deepseek = runner.G11Ledger.create(
        path=work / "deepseek.json",
        model_id=prereg.DEEPSEEK_MODEL_ID,
        ceiling=prereg.MODEL_BUDGETS[prereg.DEEPSEEK_MODEL_ID],
    )
    assert glm.ceiling == Decimal("40.00")
    assert deepseek.ceiling == Decimal("60.00")
    runner.G11Ledger.open(path=work / "glm.json", model_id=prereg.GLM_MODEL_ID)
    with pytest.raises(runner.G11RunnerError):
        runner.G11Ledger.open(path=work / "glm.json", model_id=prereg.DEEPSEEK_MODEL_ID)


def test_token_ceilings_and_effort_reach_the_provider_request():
    assert prereg.TRANSLATION_COMPLETION_CEILING == 24000
    assert prereg.AUXILIARY_COMPLETION_CEILING == 8000
    for model_id in prereg.MODEL_ORDER:
        translation = runner.normalize_g11_request(
            model_id=model_id,
            logical_operation="translation",
            request_kwargs={
                "model": prereg.PROVIDER_MODEL_IDS[model_id],
                "messages": [{"role": "user", "content": "x"}],
                "max_tokens": 12000,
                "extra_body": {"thinking": {"type": "disabled"}},
            },
        )
        assert translation["max_completion_tokens"] == TRANSLATION_TOKENS
        assert translation["reasoning_effort"] == "high"
        assert translation["model"] == prereg.PROVIDER_MODEL_IDS[model_id]

        structured = runner.normalize_g11_request(
            model_id=model_id,
            logical_operation="state_extraction",
            request_kwargs={
                "model": prereg.PROVIDER_MODEL_IDS[model_id],
                "messages": [{"role": "user", "content": "x"}],
                "max_tokens": 3000,
                "extra_body": {"thinking": {"type": "disabled"}},
            },
        )
        assert structured["max_completion_tokens"] == 3000
        assert structured["reasoning_effort"] == "low"

        semantic = runner.normalize_g11_request(
            model_id=model_id,
            logical_operation="periodic_audit",
            request_kwargs={
                "model": prereg.PROVIDER_MODEL_IDS[model_id],
                "messages": [{"role": "user", "content": "x"}],
                "max_tokens": 12000,
                "extra_body": {"thinking": {"type": "disabled"}},
            },
        )
        assert semantic["max_completion_tokens"] == AUX_TOKENS
        assert semantic["reasoning_effort"] == "high"


def test_raw_provider_sdk_retries_are_disabled(env):
    responder = lambda kwargs: _response(kwargs)
    report, _completions, factory, _seen, _delays = _execute(
        env, GLM_ACE, responder=responder, max_chapters=1
    )
    assert report["provider_calls"] > 0
    assert factory.kwargs, "the provider client factory must have been used"
    assert factory.kwargs[0]["max_retries"] == 0
    assert factory.kwargs[0]["base_url"] == "https://api.commandcode.ai/provider/v1"


# ---------------------------------------------------------------------------
# 4. Visible retries: bounded, measurable, and counted toward cost
# ---------------------------------------------------------------------------


def test_retry_wrapper_performs_at_most_three_physical_attempts(env):
    def always_transient(_kwargs):
        raise _TransportError("upstream exploded", status_code=500)

    report, completions, _factory, _seen, delays = _execute(
        env, GLM_ACE, responder=always_transient, max_chapters=1
    )
    assert report["status"] == "AMBIGUOUS_STOPPED"
    ledger = _ledger(env, prereg.GLM_MODEL_ID)
    attempts = _attempts_for(ledger, "translation")
    assert len(attempts) == prereg.MAX_TRANSPORT_ATTEMPTS == 3
    assert [attempt["attempt_index"] for attempt in attempts] == [1, 2, 3]
    assert all(attempt["classification"] == "transient" for attempt in attempts)
    assert len(completions.calls) == 3
    assert delays == list(prereg.RETRY_BACKOFF_SECONDS)
    assert ledger.summary()["unresolved_operations"] == 1


def test_transient_failure_then_success_is_recorded_visibly(env):
    state = {"calls": 0}

    def flaky(kwargs):
        state["calls"] += 1
        if state["calls"] == 1:
            raise _TransportError("rate limited", status_code=429)
        return _response(kwargs)

    _report, _completions, _factory, _seen, delays = _execute(
        env, GLM_ACE, responder=flaky, max_chapters=1
    )
    ledger = _ledger(env, prereg.GLM_MODEL_ID)
    translation = _reservations(ledger, "translation")
    assert len(translation) == 1
    assert translation[0]["first_attempt_success"] is False
    assert translation[0]["eventual_success"] is True
    assert translation[0]["transport_attempts"] == 2
    assert translation[0]["status"] == "settled"
    assert delays == [2]
    summary = ledger.summary()
    assert summary["retries"] == 1
    assert summary["transport_attempts"] == 3
    assert summary["first_attempt_successes"] == 1  # state_extraction only
    assert summary["eventual_successes"] == 2


def test_deterministic_4xx_is_not_retried(env):
    def invalid_request(_kwargs):
        raise _TransportError("bad request", status_code=400)

    report, completions, _factory, _seen, delays = _execute(
        env, GLM_ACE, responder=invalid_request, max_chapters=1
    )
    assert report["status"] == "TERMINAL_FAILURE"
    ledger = _ledger(env, prereg.GLM_MODEL_ID)
    attempts = _attempts_for(ledger, "translation")
    assert len(attempts) == 1
    assert attempts[0]["classification"] == "deterministic"
    assert len(completions.calls) == 1
    assert delays == []
    assert _reservations(ledger, "translation")[0]["status"] == "abandoned_no_usage"


def test_transport_failure_classification():
    assert runner.classify_transport_failure(
        _TransportError("bad", status_code=400)
    ) == "deterministic"
    assert runner.classify_transport_failure(
        _TransportError("auth", status_code=401)
    ) == "deterministic"
    assert runner.classify_transport_failure(
        _TransportError("banned", status_code=403)
    ) == "deterministic"
    assert runner.classify_transport_failure(
        _TransportError("timeout", status_code=408)
    ) == "transient"
    assert runner.classify_transport_failure(
        _TransportError("limited", status_code=429)
    ) == "transient"
    assert runner.classify_transport_failure(
        _TransportError("boom", status_code=503)
    ) == "transient"
    assert runner.classify_transport_failure(ConnectionError("reset")) == "transient"
    assert runner.classify_transport_failure(TimeoutError("slow")) == "transient"
    assert runner.classify_transport_failure(ValueError("nonsense")) == "deterministic"


def test_every_physical_attempt_is_recorded_and_its_cost_is_counted(env):
    state = {"calls": 0}

    def flaky(kwargs):
        state["calls"] += 1
        if state["calls"] == 1:
            raise _TransportError("timed out", status_code=408)
        return _response(kwargs, prompt=200, completion=50)

    _report, _completions, _factory, _seen, _delays = _execute(
        env, GLM_ACE, responder=flaky, max_chapters=1
    )
    ledger = _ledger(env, prereg.GLM_MODEL_ID)
    attempts = _attempts_for(ledger, "translation")
    assert [attempt["outcome"] for attempt in attempts] == [
        "transient_failure",
        "success",
    ]
    assert Decimal(attempts[0]["credit_equivalent"]) == 0

    settled = [
        record
        for record in ledger.document["reservations"]
        if record["status"] == "settled"
    ]
    assert len(settled) == 2
    assert all(Decimal(record["credit_equivalent"]) > 0 for record in settled)
    assert ledger.committed_credit_equivalent() == sum(
        (Decimal(record["credit_equivalent"]) for record in settled), Decimal(0)
    )
    # The retried translation's accepted cost is inside committed CE.
    assert ledger.committed_credit_equivalent() > Decimal(
        _reservations(ledger, "state_extraction")[0]["credit_equivalent"]
    )


def test_ledger_enforces_hard_ceiling_and_unresolved_state(work):
    ledger = runner.G11Ledger.create(
        path=work / "ledger.json",
        model_id=prereg.GLM_MODEL_ID,
        ceiling="0.5",
    )
    ledger.ensure_budget_available()
    reservation_id = ledger.begin_operation(
        operation="translation",
        request_sha256="a" * 64,
        run_id=GLM_ACE,
        novel_id="ace_evolution",
        chapter=1,
        regeneration=False,
    )
    with pytest.raises(runner.G11BudgetError):
        ledger.settle(
            reservation_id=reservation_id,
            stats={
                "credit_equivalent": "0.6",
                "pricing_version": prereg.G11_PRICING_VERSION,
            },
        )
    assert ledger.document["budget_violation"] is True

    other = runner.G11Ledger.create(
        path=work / "ledger2.json",
        model_id=prereg.DEEPSEEK_MODEL_ID,
        ceiling="60.00",
    )
    unresolved = other.begin_operation(
        operation="translation",
        request_sha256="b" * 64,
        run_id=DEEPSEEK_ACE,
        novel_id="ace_evolution",
        chapter=1,
        regeneration=False,
    )
    other.mark_unresolved(reservation_id=unresolved, reason="exhausted")
    with pytest.raises(runner.G11BudgetError):
        other.ensure_budget_available()


# ---------------------------------------------------------------------------
# 5. Translation regeneration
# ---------------------------------------------------------------------------


def test_translation_length_allows_exactly_one_regeneration(env):
    state = {"translation_calls": 0}

    def responder(kwargs):
        if kwargs.get("max_completion_tokens") == TRANSLATION_TOKENS:
            state["translation_calls"] += 1
            if state["translation_calls"] == 1:
                return _response(kwargs, content="", finish_reason="length")
            return _response(kwargs, content="REGENERATED TRANSLATION")
        return _response(kwargs, content="aux content")

    report, _completions, _factory, seen, _delays = _execute(
        env, GLM_ACE, responder=responder, max_chapters=1
    )
    assert report["status"] in {"STOPPED_BEFORE_COMPLETION", "COMPLETED"}
    kinds = _journal_kinds(env, GLM_ACE)
    assert "g11_translation" in kinds
    assert any(kind.startswith("g11_translation_regen") for kind in kinds)
    ledger = _ledger(env, prereg.GLM_MODEL_ID)
    assert ledger.summary()["regenerations"] == 1
    regenerations = [
        record for record in _reservations(ledger, "translation") if record["regeneration"]
    ]
    assert len(regenerations) == 1
    assert state["translation_calls"] == 2
    assert seen["translations"][1] == "REGENERATED TRANSLATION"


def test_regeneration_triggers_are_only_length_or_effectively_empty():
    assert runner.regeneration_reason(
        {"finish_reason": "length", "content": "full text"}
    ) == "finish_reason_length"
    assert runner.regeneration_reason(
        {"finish_reason": "stop", "content": "   \n  "}
    ) == "empty_or_blank_content"
    assert runner.regeneration_reason(
        {"finish_reason": "stop", "content": "good text"}
    ) == ""
    assert prereg.REPAIR_POLICY["translation_regeneration_limit"] == 1
    assert prereg.REPAIR_POLICY["new_heuristic_quality_gates_added"] is False


# ---------------------------------------------------------------------------
# 6. Failure isolation and resume
# ---------------------------------------------------------------------------


def test_exhausted_failure_stops_only_its_track_and_other_track_continues(env):
    def responder(kwargs):
        if "ace_evolution" in kwargs["messages"][-1]["content"]:
            raise _TransportError("invalid request", status_code=400)
        return _response(kwargs, content="ok")

    completions = _FakeCompletions(responder)
    factory = _FakeFactory(completions)
    seen = {}

    def loader(*, code_root, track_root_path):
        run_id = Path(track_root_path).name
        module = _fake_module(
            prereg.TRACK_BY_ID[run_id], Path(track_root_path), audit=True, seen=seen
        )
        seen[run_id] = module
        return {"module": module}

    result = runner.run_g11_unattended(
        execution_root=env["execution"],
        api_key=API_KEY,
        tracks=(GLM_ACE, GLM_ROGUE),
        max_chapters=2,
        client_factory=factory,
        production_module_loader=loader,
        sleep=lambda _seconds: None,
    )
    assert result["tracks"][GLM_ACE]["status"] == "TERMINAL_FAILURE"
    assert result["tracks"][GLM_ACE]["other_tracks_unaffected"] is True
    assert result["tracks"][GLM_ROGUE]["last_completed_chapter"] == 2
    assert runner.load_quarantine(_track_root(env, GLM_ACE))["outcome"] == (
        "terminal_failure"
    )
    assert runner.load_quarantine(_track_root(env, GLM_ROGUE)) is None


def test_quarantined_track_never_advances_or_spends_on_resume(env):
    def responder(kwargs):
        if "ace_evolution" in kwargs["messages"][-1]["content"]:
            raise _TransportError("invalid request", status_code=400)
        return _response(kwargs, content="ok")

    completions = _FakeCompletions(responder)
    factory = _FakeFactory(completions)
    seen = {}

    def loader(*, code_root, track_root_path):
        module = _fake_module(
            prereg.TRACK_BY_ID[GLM_ACE],
            Path(track_root_path),
            audit=True,
            seen=seen,
        )
        return {"module": module}

    first = runner.run_g11_track(
        execution_root=env["execution"],
        run_id=GLM_ACE,
        api_key=API_KEY,
        client_factory=factory,
        production_module_loader=loader,
        max_chapters=1,
        sleep=lambda _seconds: None,
    )
    assert first["status"] == "TERMINAL_FAILURE"
    calls_after_first = len(completions.calls)
    second = runner.run_g11_track(
        execution_root=env["execution"],
        run_id=GLM_ACE,
        api_key=API_KEY,
        client_factory=factory,
        production_module_loader=loader,
        max_chapters=1,
        sleep=lambda _seconds: None,
    )
    assert second["status"] == "QUARANTINED_STOPPED"
    assert second["provider_calls"] == 0
    assert len(completions.calls) == calls_after_first


def test_resume_continues_from_durable_state_without_double_accounting(env):
    responder = lambda kwargs: _response(kwargs, content="ok")
    first, _completions, _factory, _seen, _delays = _execute(
        env, GLM_ACE, responder=responder, max_chapters=1
    )
    assert first["last_completed_chapter"] == 1
    committed_after_first = _ledger(
        env, prereg.GLM_MODEL_ID
    ).committed_credit_equivalent()

    second, _completions, _factory, _seen, _delays = _execute(
        env, GLM_ACE, responder=responder, max_chapters=1
    )
    assert second["last_completed_chapter"] == 2
    assert second["chapters_committed"] == [2]
    ledger = _ledger(env, prereg.GLM_MODEL_ID)
    assert ledger.committed_credit_equivalent() > committed_after_first
    # Nothing for Chapter 1 was re-sent or re-accounted.
    chapter_one = [
        record
        for record in ledger.document["reservations"]
        if record["chapter"] == 1
    ]
    assert len(chapter_one) == 2


def test_per_chapter_invocation_ledgers_are_per_chapter(env):
    responder = lambda kwargs: _response(kwargs, content="ok")
    _execute(env, GLM_ACE, responder=responder, max_chapters=2)
    root = _track_root(env, GLM_ACE)
    first = json.loads(
        (root / "data" / "g11_invocations" / "0001.json").read_text(encoding="utf-8")
    )
    second = json.loads(
        (root / "data" / "g11_invocations" / "0002.json").read_text(encoding="utf-8")
    )
    assert first["chapter"] == 1
    assert second["chapter"] == 2
    # translation + state_extraction for each chapter, not a running total.
    assert first["provider_transport_attempts"] == 2
    assert second["provider_transport_attempts"] == 2
    assert first["observed_subrequest_count"] == 2
    assert second["observed_subrequest_count"] == 2
    assert second["automatic_chapter_advance"] is False
    assert second["synthetic_audit_inserted"] is False


def test_missing_api_key_authorizes_no_provider_call(env):
    reports = runner.run_g11_unattended(
        execution_root=env["execution"],
        api_key=None,
        tracks=(GLM_ACE,),
    )
    assert reports["tracks"][GLM_ACE]["status"] == "AUTHORIZATION_REQUIRED"
    assert reports["provider_calls"] == 0


# ---------------------------------------------------------------------------
# 7. Audits stay engine-owned; checkpoints are read-only evidence
# ---------------------------------------------------------------------------


def test_runner_never_inserts_a_synthetic_audit(env):
    responder = lambda kwargs: _response(kwargs, content="ok")
    report, _completions, _factory, _seen, _delays = _execute(
        env, GLM_ACE, responder=responder, max_chapters=26, audit=False
    )
    progress = runner.read_track_progress(_track_root(env, GLM_ACE))
    assert progress["last_completed"] == 26
    # The scripted engine never audited; the runner must not audit for it.
    assert progress["last_audited_chapter"] == 0
    assert report["synthetic_audits_inserted"] is False
    assert not any("periodic_audit" in kind for kind in _journal_kinds(env, GLM_ACE))


def test_engine_owned_audit_is_journaled_and_frozen_as_state_evidence(env):
    responder = lambda kwargs: _response(kwargs, content="ok")
    report, _completions, _factory, _seen, _delays = _execute(
        env, GLM_ACE, responder=responder, max_chapters=25, audit=True
    )
    progress = runner.read_track_progress(_track_root(env, GLM_ACE))
    assert progress["last_completed"] == 25
    assert progress["last_audited_chapter"] == 25
    assert any(
        kind.startswith("g11_periodic_audit") for kind in _journal_kinds(env, GLM_ACE)
    )
    evidence = env["execution"] / runner.G11_EVIDENCE_DIRECTORY / GLM_ACE / "0025"
    checkpoint = json.loads((evidence / "checkpoint.json").read_text(encoding="utf-8"))
    assert checkpoint["state_evidence_checkpoint"] is True
    assert checkpoint["quality_checkpoint"] is True
    assert (evidence / "audit_history.json").is_file()
    assert report["chapters_committed"][-1] == 25


def test_quality_checkpoint_is_frozen_without_mutating_the_track(env):
    responder = lambda kwargs: _response(kwargs, content="ok")
    _execute(env, GLM_ACE, responder=responder, max_chapters=1)
    evidence = env["execution"] / runner.G11_EVIDENCE_DIRECTORY / GLM_ACE / "0001"
    checkpoint = json.loads((evidence / "checkpoint.json").read_text(encoding="utf-8"))
    assert checkpoint["quality_checkpoint"] is True
    assert checkpoint["state_evidence_checkpoint"] is False
    assert checkpoint["track_unchanged_by_extraction"] is True
    assert (evidence / "chapter.md").is_file()
    assert (evidence / "progress.json").is_file()
    assert checkpoint["artifacts"]["chapter"]["sha256"] == hashlib.sha256(
        (evidence / "chapter.md").read_bytes()
    ).hexdigest()

    root = _track_root(env, GLM_ACE)
    before = _tree_snapshot(root)
    result = runner.freeze_checkpoints_for_chapter(
        execution_root=env["execution"], run_id=GLM_ACE, chapter=10
    )
    after = _tree_snapshot(root)
    assert result["frozen"] is True
    assert before == after, "checkpoint extraction must not mutate the track"
    assert (
        env["execution"] / runner.G11_EVIDENCE_DIRECTORY / GLM_ACE / "0010"
    ).is_dir()


def test_quality_checkpoint_records_both_models_and_both_novels(env):
    responder = lambda kwargs: _response(kwargs, content="ok")
    seen = {}
    completions = _FakeCompletions(responder)
    factory = _FakeFactory(completions)

    def loader(*, code_root, track_root_path):
        run_id = Path(track_root_path).name
        return {
            "module": _fake_module(
                prereg.TRACK_BY_ID[run_id],
                Path(track_root_path),
                audit=False,
                seen=seen,
            )
        }

    runner.run_g11_unattended(
        execution_root=env["execution"],
        api_key=API_KEY,
        max_chapters=1,
        client_factory=factory,
        production_module_loader=loader,
        sleep=lambda _seconds: None,
    )
    for track in prereg.TRACKS:
        evidence = (
            env["execution"]
            / runner.G11_EVIDENCE_DIRECTORY
            / track["run_id"]
            / "0001"
            / "checkpoint.json"
        )
        assert evidence.is_file()
        payload = json.loads(evidence.read_text(encoding="utf-8"))
        assert payload["model_id"] == track["model_id"]
        assert payload["novel_id"] == track["novel_id"]


# ---------------------------------------------------------------------------
# 8. Isolation: production root and pristine S0 template stay untouched
# ---------------------------------------------------------------------------


def test_production_root_is_never_mutated(env):
    before = _tree_snapshot(env["production"])
    responder = lambda kwargs: _response(kwargs, content="ok")
    _execute(env, GLM_ACE, responder=responder, max_chapters=2)
    assert before != []
    assert _tree_snapshot(env["production"]) == before


def test_s0_root_is_never_mutated_and_verify_proves_it(env):
    before = _tree_snapshot(env["pristine"])
    responder = lambda kwargs: _response(kwargs, content="ok")
    _execute(env, GLM_ACE, responder=responder, max_chapters=2)
    proof = runner.verify_g11(
        execution_root=env["execution"],
        pristine_root=env["pristine"],
        production_root=env["production"],
    )
    assert _tree_snapshot(env["pristine"]) == before
    assert proof["pristine_proof"]["unchanged"] is True
    assert proof["residual_repair_enabled_in_all_copies"] is True
    assert proof["provider_calls"] == 0


def test_execution_root_must_be_fresh_and_external(env):
    with pytest.raises(runner.G11RunnerError):
        runner.prepare_g11(
            execution_root=env["execution"],
            pristine_root=env["pristine"],
        )
    with pytest.raises(G10SafetyError):
        runner.prepare_g11(
            execution_root=REPO_ROOT / "g11_should_never_exist",
            pristine_root=env["pristine"],
        )
    assert not (REPO_ROOT / "g11_should_never_exist").exists()


def test_execution_root_may_not_overlap_the_pristine_s0_template(env):
    with pytest.raises(G10SafetyError):
        runner.prepare_g11(
            execution_root=env["pristine"] / "nested_execution",
            pristine_root=env["pristine"],
        )


# ---------------------------------------------------------------------------
# 9. Status is read-only and reports zero provider calls
# ---------------------------------------------------------------------------


def test_status_g11_is_read_only(env):
    responder = lambda kwargs: _response(kwargs, content="ok")
    _execute(env, GLM_ACE, responder=responder, max_chapters=1)
    before = _tree_snapshot(env["execution"])
    status = runner.status_g11(execution_root=env["execution"])
    assert _tree_snapshot(env["execution"]) == before
    assert status["provider_calls"] == 0
    assert len(status["tracks"]) == 4
    ace = [item for item in status["tracks"] if item["run_id"] == GLM_ACE][0]
    assert ace["last_completed_chapter"] == 1
    assert ace["ledger"]["ceiling_credit_equivalent"] == "40.00"
    assert ace["frozen_checkpoints"] == [1]


def test_status_reports_not_prepared_without_touching_anything(work):
    status = runner.status_g11(execution_root=work / "absent")
    assert status["status"] == "NOT_PREPARED"
    assert status["provider_calls"] == 0
    assert not (work / "absent").exists()


# ---------------------------------------------------------------------------
# 10. No credentials anywhere in the exercised paths
# ---------------------------------------------------------------------------


def test_no_credentials_are_accessed_during_prepare_and_run(env, monkeypatch):
    observed = []
    real_getenv = os.getenv

    def spy(name, default=None):
        observed.append(name)
        if name in CREDENTIAL_ENV_NAMES:
            raise AssertionError(f"G11 must not read credential env var {name!r}")
        return real_getenv(name, default)

    monkeypatch.setattr(os, "getenv", spy)
    responder = lambda kwargs: _response(kwargs, content="ok")
    _execute(env, GLM_ROGUE, responder=responder, max_chapters=1)
    assert not [name for name in observed if name in CREDENTIAL_ENV_NAMES]


def test_g11_modules_never_load_dotenv_or_read_the_environment():
    for name in (
        "benchmark_longitudinal_g11_preregistration.py",
        "benchmark_longitudinal_g11_runner.py",
    ):
        source = (REPO_ROOT / name).read_text(encoding="utf-8")
        assert "load_dotenv" not in source
        assert "getenv" not in source
        assert "dotenv" not in source


def test_runner_never_reads_a_credential_from_the_environment():
    # The launcher injects the API key; the runner must not resolve one itself.
    import inspect

    source = inspect.getsource(runner)
    assert "os.environ" not in source
    assert "DEEPSEEK_API_KEY" not in source
    assert "OPENAI_API_KEY" not in source


# ---------------------------------------------------------------------------
# 11. Deterministic content recovery and safe quarantine reopen
# ---------------------------------------------------------------------------


def test_content_stop_reason_classification():
    assert runner.is_content_stop_reason("SystemExit: 1")
    assert runner.is_content_stop_reason(
        "G11RunnerError: the real lifecycle did not finish exactly the "
        "expected chapter: last_completed=6, target=7"
    )
    assert not runner.is_content_stop_reason("G11BudgetError: ceiling reached")
    assert not runner.is_content_stop_reason(
        "G11ContentRecoveryExhausted: content recovery exhausted for chapter 7"
    )
    assert not runner.is_content_stop_reason(None)


def test_ambiguous_quarantine_is_never_reopened(env):
    responder = lambda kwargs: _response(kwargs, content="ok")
    _seed_pending(env, GLM_ACE, 1)
    _seed_quarantine(
        env,
        GLM_ACE,
        first_blocked_chapter=1,
        outcome="ambiguous_remote_outcome",
        reason="G11AmbiguousOutcome: paid subrequest may have completed remotely",
    )
    report, completions, _factory, _seen, _delays = _execute(
        env, GLM_ACE, responder=responder, max_chapters=1
    )
    assert report["status"] == "QUARANTINED_STOPPED"
    assert report["provider_calls"] == 0
    assert completions.calls == []
    assert report["quarantine_reopen"]["reason"] == "outcome_not_terminal_failure"
    assert runner.load_quarantine(_track_root(env, GLM_ACE)) is not None


def test_uncertain_journal_prevents_quarantine_reopen(env):
    responder = lambda kwargs: _response(kwargs, content="ok")
    _seed_pending(env, GLM_ACE, 1)
    _seed_uncertain_journal(env, GLM_ACE, 1)
    _seed_quarantine(env, GLM_ACE, first_blocked_chapter=1, reason="SystemExit: 1")
    report, completions, _factory, _seen, _delays = _execute(
        env, GLM_ACE, responder=responder, max_chapters=1
    )
    assert report["status"] == "QUARANTINED_STOPPED"
    assert report["quarantine_reopen"]["reason"] == "unresolved_provider_journals"
    assert completions.calls == []
    assert runner.load_quarantine(_track_root(env, GLM_ACE)) is not None


def test_quarantine_advanced_past_block_is_not_reopened(env):
    _seed_progress(env, GLM_ACE, last_completed=5)
    _seed_pending(env, GLM_ACE, 6)
    _seed_quarantine(env, GLM_ACE, first_blocked_chapter=4, reason="SystemExit: 1")
    quarantine = runner.load_quarantine(_track_root(env, GLM_ACE))
    decision = runner.classify_quarantine_reopen(
        _track_root(env, GLM_ACE), quarantine, expected_run_id=GLM_ACE
    )
    assert decision["reopenable"] is False
    assert decision["reason"] == "progress_advanced_past_block"


def test_exact_content_stop_quarantine_reopens_and_archives_evidence(env):
    responder = lambda kwargs: _response(kwargs, content="recovered translation")
    pending = _seed_pending(env, GLM_ACE, 1, text="rejected body\n")
    journal = _seed_translation_journal(env, GLM_ACE, 1)
    originals = {path.name: path.read_bytes() for path in [*pending, journal]}
    _seed_quarantine(env, GLM_ACE, first_blocked_chapter=1, reason="SystemExit: 1")

    report, completions, _factory, _seen, _delays = _execute(
        env, GLM_ACE, responder=responder, max_chapters=1
    )

    assert report["last_completed_chapter"] == 1
    assert completions.calls, "the reopened track must run a real lifecycle"
    assert report["quarantine_reopened"]["blocked_chapter"] == 1
    assert runner.load_quarantine(_track_root(env, GLM_ACE)) is None
    archived_marker = Path(report["quarantine_reopened"]["archived_marker"])
    assert archived_marker.is_file()
    assert json.loads(archived_marker.read_text(encoding="utf-8"))["outcome"] == (
        "terminal_failure"
    )

    cycle = runner.content_recovery_directory(_track_root(env, GLM_ACE), 1) / "cycle_01"
    assert cycle.is_dir()
    for name, payload in originals.items():
        assert (cycle / name).read_bytes() == payload
    # The rejected pending must be gone; the paid journal slot is legitimately
    # recreated by the fresh recovery lifecycle.
    assert not any(path.exists() for path in pending)
    manifest = json.loads((cycle / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["chapter"] == 1
    assert manifest["cycle"] == 1
    assert manifest["model_id"] == prereg.GLM_MODEL_ID
    assert report["content_recovery_cycles"] == 1


def test_runner_content_stop_marker_reason_also_reopens(env):
    responder = lambda kwargs: _response(kwargs, content="ok")
    _seed_pending(env, GLM_ACE, 1)
    _seed_quarantine(
        env,
        GLM_ACE,
        first_blocked_chapter=1,
        reason=(
            "G11RunnerError: the real lifecycle did not finish exactly the "
            "expected chapter: last_completed=0, target=1"
        ),
    )
    report, _completions, _factory, _seen, _delays = _execute(
        env, GLM_ACE, responder=responder, max_chapters=1
    )
    assert report["quarantine_reopened"]["blocked_chapter"] == 1
    assert report["last_completed_chapter"] == 1


def test_content_recovery_retries_same_chapter_then_continues(env):
    responder = lambda kwargs: _response(kwargs, content="usable translation")
    report, _completions, _factory, seen, _delays = _execute(
        env, GLM_ACE, responder=responder, max_chapters=2, qa_block_attempts=((1, 1),)
    )
    assert report["chapters_committed"] == [1, 2]
    assert report["last_completed_chapter"] == 2
    assert report["content_recovery_cycles"] == 1
    assert seen["chapters"].count(1) == 2, "the same chapter is retried once"
    cycle = runner.content_recovery_directory(_track_root(env, GLM_ACE), 1) / "cycle_01"
    assert (cycle / "0001.md").is_file()
    assert (cycle / "0001.json").is_file()
    assert (cycle / "0001.g11_translation.json").is_file()


def test_content_recovery_is_bounded_to_two_cycles(env):
    responder = lambda kwargs: _response(kwargs, content="usable translation")
    report, _completions, _factory, seen, _delays = _execute(
        env, GLM_ACE, responder=responder, max_chapters=1, qa_block_attempts=((1, 99),)
    )
    assert report["status"] == "TERMINAL_FAILURE"
    assert report["error"]["type"] == "G11ContentRecoveryExhausted"
    assert report["content_recovery_cycles"] == 2
    assert seen["chapters"].count(1) == 3, "one attempt plus two recovery cycles"
    cycles = runner.content_recovery_directory(_track_root(env, GLM_ACE), 1)
    assert (cycles / "cycle_01").is_dir()
    assert (cycles / "cycle_02").is_dir()
    assert not (cycles / "cycle_03").exists()
    quarantine = runner.load_quarantine(_track_root(env, GLM_ACE))
    assert runner.is_content_stop_reason(quarantine["reason"]) is False
    assert runner.classify_quarantine_reopen(
        _track_root(env, GLM_ACE), quarantine, expected_run_id=GLM_ACE
    )["reopenable"] is False


def test_glm_recovery_stays_high_effort_with_recovery_instruction(env):
    responder = lambda kwargs: _response(kwargs, content="usable translation")
    report, completions, _factory, _seen, _delays = _execute(
        env, GLM_ACE, responder=responder, max_chapters=1, qa_block_attempts=((1, 1),)
    )
    assert report["deepseek_adaptive_low_mode"]["activated"] is False
    calls = _translation_calls_for_chapter(completions, 1)
    assert calls and all(call["reasoning_effort"] == "high" for call in calls)
    recovery_calls = [
        call
        for call in calls
        if any(
            runner.G11_RECOVERY_TRANSLATION_INSTRUCTION in message.get("content", "")
            for message in call["messages"]
        )
    ]
    assert recovery_calls, "the recovery translation must carry the recovery instruction"


def test_recovery_never_resets_old_ledger_entries_or_costs(env):
    responder = lambda kwargs: _response(kwargs, content="usable")
    _execute(env, GLM_ACE, responder=responder, max_chapters=1)
    ledger = _ledger(env, prereg.GLM_MODEL_ID)
    before_reservations = copy.deepcopy(ledger.document["reservations"])
    before_committed = ledger.committed_credit_equivalent()
    assert before_committed > 0

    report, _completions, _factory, _seen, _delays = _execute(
        env, GLM_ACE, responder=responder, max_chapters=1, qa_block_attempts=((2, 1),)
    )
    assert report["content_recovery_cycles"] == 1
    assert report["last_completed_chapter"] == 2

    after = _ledger(env, prereg.GLM_MODEL_ID)
    for record in before_reservations:
        assert record in after.document["reservations"]
    assert after.committed_credit_equivalent() > before_committed


def test_deepseek_high_saturation_triggers_low_effort_regeneration(env):
    def responder(kwargs):
        if kwargs.get("reasoning_effort") == "high":
            return _response(kwargs, content="", finish_reason="length")
        return _response(kwargs, content="LOW EFFORT TRANSLATION")

    report, completions, _factory, seen, _delays = _execute(
        env, DEEPSEEK_ACE, responder=responder, max_chapters=1
    )
    assert report["last_completed_chapter"] == 1
    assert report["translation_regenerations"] == 1
    assert report["deepseek_adaptive_low_mode"]["activated"] is True
    assert report["deepseek_adaptive_low_mode"]["activation_chapter"] == 1
    assert report["deepseek_adaptive_low_mode"]["high_effort_saturation_events"] == 1
    efforts = [call["reasoning_effort"] for call in _translation_calls_for_chapter(completions, 1)]
    assert efforts[0] == "high"
    assert efforts[-1] == "low"
    assert seen["translations"][1] == "LOW EFFORT TRANSLATION"
    marker = runner.load_deepseek_low_mode(
        runner.deepseek_low_mode_path(_track_root(env, DEEPSEEK_ACE))
    )
    assert marker["activation_chapter"] == 1
    assert runner.deepseek_low_mode_summary(marker)["activated"] is True


def test_deepseek_low_mode_persists_for_later_chapters(env):
    def responder(kwargs):
        if kwargs.get("reasoning_effort") == "high" and any(
            "translate chapter 1" in message.get("content", "")
            for message in kwargs["messages"]
        ):
            return _response(kwargs, content="", finish_reason="length")
        return _response(kwargs, content="TRANSLATION")

    report, completions, _factory, _seen, _delays = _execute(
        env, DEEPSEEK_ACE, responder=responder, max_chapters=2
    )
    assert report["last_completed_chapter"] == 2
    assert report["deepseek_adaptive_low_mode"]["activation_chapter"] == 1
    chapter_two = _translation_calls_for_chapter(completions, 2)
    assert chapter_two and all(call["reasoning_effort"] == "low" for call in chapter_two)


def test_deepseek_low_mode_leaves_semantic_high_and_structured_low(env):
    _seed_progress(env, DEEPSEEK_ACE, last_completed=24, last_audited_chapter=0)
    runner.activate_deepseek_low_mode(
        path=runner.deepseek_low_mode_path(_track_root(env, DEEPSEEK_ACE)),
        run_id=DEEPSEEK_ACE,
        model_id=prereg.DEEPSEEK_MODEL_ID,
        chapter=24,
        reason="test_seed",
    )
    responder = lambda kwargs: _response(kwargs, content="ok")
    report, completions, _factory, _seen, _delays = _execute(
        env, DEEPSEEK_ACE, responder=responder, max_chapters=1, audit=True
    )
    assert report["last_completed_chapter"] == 25

    translation = _translation_calls_for_chapter(completions, 25)
    assert translation and all(call["reasoning_effort"] == "low" for call in translation)
    audits = [
        call
        for call in completions.calls
        if any("audit chunk" in message.get("content", "") for message in call["messages"])
    ]
    assert audits and all(call["reasoning_effort"] == "high" for call in audits)
    structured = [
        call
        for call in completions.calls
        if any(
            "extract state" in message.get("content", "") for message in call["messages"]
        )
    ]
    assert structured and all(call["reasoning_effort"] == "low" for call in structured)


def test_reopened_deepseek_chapter_uses_low_mode_before_first_recovery_send(env):
    responder = lambda kwargs: _response(kwargs, content="usable")
    _seed_progress(env, DEEPSEEK_ACE, last_completed=3, last_audited_chapter=0)
    _seed_translation_journal(env, DEEPSEEK_ACE, 4, content="", finish_reason="length")
    _seed_quarantine(env, DEEPSEEK_ACE, first_blocked_chapter=4, reason="SystemExit: 1")

    def guarded_responder(kwargs):
        assert kwargs.get("reasoning_effort") == "low", (
            "no HIGH recovery send is permitted after saturation evidence"
        )
        return _response(kwargs, content="LOW RECOVERED TRANSLATION")

    report, completions, _factory, _seen, _delays = _execute(
        env, DEEPSEEK_ACE, responder=guarded_responder, max_chapters=1
    )
    assert report["last_completed_chapter"] == 4
    assert report["quarantine_reopened"]["blocked_chapter"] == 4
    assert report["deepseek_adaptive_low_mode"]["activated"] is True
    assert report["deepseek_adaptive_low_mode"]["activation_chapter"] == 4
    calls = _translation_calls_for_chapter(completions, 4)
    assert calls and all(call["reasoning_effort"] == "low" for call in calls)


def test_adaptive_low_mode_does_not_change_transport_retry_policy(env):
    runner.activate_deepseek_low_mode(
        path=runner.deepseek_low_mode_path(_track_root(env, DEEPSEEK_ACE)),
        run_id=DEEPSEEK_ACE,
        model_id=prereg.DEEPSEEK_MODEL_ID,
        chapter=1,
        reason="test_seed",
    )

    def always_transient(_kwargs):
        raise _TransportError("upstream exploded", status_code=500)

    report, completions, _factory, _seen, delays = _execute(
        env, DEEPSEEK_ACE, responder=always_transient, max_chapters=1
    )
    assert report["status"] == "AMBIGUOUS_STOPPED"
    assert len(completions.calls) == prereg.MAX_TRANSPORT_ATTEMPTS == 3
    assert delays == list(prereg.RETRY_BACKOFF_SECONDS)
    ledger = _ledger(env, prereg.DEEPSEEK_MODEL_ID)
    attempts = _attempts_for(ledger, "translation")
    assert [attempt["attempt_index"] for attempt in attempts] == [1, 2, 3]
    assert all(attempt["classification"] == "transient" for attempt in attempts)
    assert attempts[0]["credit_equivalent"] == "0"


def test_status_exposes_adaptive_metrics(env):
    def responder(kwargs):
        if kwargs.get("reasoning_effort") == "high":
            return _response(kwargs, content="", finish_reason="length")
        return _response(kwargs, content="low translation")

    _execute(env, DEEPSEEK_ACE, responder=responder, max_chapters=1)
    status = runner.status_g11(execution_root=env["execution"])
    entry = [track for track in status["tracks"] if track["run_id"] == DEEPSEEK_ACE][0]
    assert entry["deepseek_low_translation_mode"]["activated"] is True
    assert entry["deepseek_low_translation_mode"]["activation_chapter"] == 1
    assert entry["deepseek_low_translation_mode"]["high_effort_saturation_events"] == 1
    assert entry["content_recovery_cycles"] == 0
    assert status["provider_calls"] == 0


def test_normalize_request_effort_override_rules():
    base = {
        "model": prereg.PROVIDER_MODEL_IDS[prereg.DEEPSEEK_MODEL_ID],
        "messages": [{"role": "user", "content": "x"}],
        "max_tokens": 12000,
        "extra_body": {"thinking": {"type": "disabled"}},
    }
    low = runner.normalize_g11_request(
        model_id=prereg.DEEPSEEK_MODEL_ID,
        logical_operation="translation",
        request_kwargs=base,
        reasoning_effort="low",
    )
    assert low["reasoning_effort"] == "low"

    with pytest.raises(runner.G11RunnerError):
        runner.normalize_g11_request(
            model_id=prereg.DEEPSEEK_MODEL_ID,
            logical_operation="periodic_audit",
            request_kwargs=base,
            reasoning_effort="low",
        )
    with pytest.raises(runner.G11RunnerError):
        runner.normalize_g11_request(
            model_id=prereg.DEEPSEEK_MODEL_ID,
            logical_operation="translation",
            request_kwargs=base,
            reasoning_effort="medium",
        )


# ---------------------------------------------------------------------------
# 12. Audit-only content recovery (final periodic glossary audit)
# ---------------------------------------------------------------------------


AUDIT_START = prereg.AUDIT_CHECKPOINTS[-2] + 1  # 76
AUDIT_END = prereg.LAST_CHAPTER  # 100
GLM_LEDGER = prereg.GLM_MODEL_ID


def _audit_failure_reason(failed_chunk="c0001"):
    return (
        "AuditChunkFailure: Periodic glossary audit stopped safely for Chapters "
        f"{AUDIT_START}-{AUDIT_END}. Failed chunk {failed_chunk} of 5; candidates: "
        "alpha, beta. Completed chunks: c0002, c0003, c0004, c0005. Reason: audit "
        "schema/semantic validation failed"
    )


def _seed_audit_checkpoint(root, *, failed=("c0001",), attempts=1):
    failed = set(failed)
    chunk_ids = ["c0001", "c0002", "c0003", "c0004", "c0005"]
    chunks = []
    records = []
    for chunk_id in chunk_ids:
        candidate_ids = [f"{chunk_id}a", f"{chunk_id}b"]
        if chunk_id in failed:
            chunks.append(
                {
                    "chunk_id": chunk_id,
                    "candidate_ids": candidate_ids,
                    "family_keys": candidate_ids,
                    "depth": 0,
                    "status": "validation_failed",
                    "attempts": attempts,
                    "last_error": "audit schema/semantic validation failed",
                }
            )
            records.append(
                {
                    "chunk_id": chunk_id,
                    "kind": "audit",
                    "attempt": attempts,
                    "operation_id": (
                        "periodic_glossary_audit_chunk:deadbeef:"
                        f"{chunk_id}:audit:{attempts}"
                    ),
                    "lifecycle_version": "audit-attempt-lifecycle-v1",
                    "lifecycle": "validation_failed",
                    "validation_errors": [
                        {"code": "bad_field", "message": "x", "retryable": False}
                    ],
                    "raw_content": '{"decisions": []}',
                    "raw_content_hash": hashlib.sha256(
                        b'{"decisions": []}'
                    ).hexdigest(),
                    "finish_reason": "stop",
                    "stats": {"prompt_tokens": 1, "completion_tokens": 1},
                }
            )
        else:
            parsed = {"decisions": [{"candidate_id": candidate_ids[0], "action": "keep"}]}
            chunks.append(
                {
                    "chunk_id": chunk_id,
                    "candidate_ids": candidate_ids,
                    "family_keys": candidate_ids,
                    "depth": 0,
                    "status": "completed",
                    "attempts": 1,
                    "result": parsed,
                }
            )
            records.append(
                {
                    "chunk_id": chunk_id,
                    "kind": "audit",
                    "attempt": 1,
                    "operation_id": (
                        f"periodic_glossary_audit_chunk:deadbeef:{chunk_id}:audit:1"
                    ),
                    "lifecycle_version": "audit-attempt-lifecycle-v1",
                    "lifecycle": "response_recorded",
                    "parsed_result": parsed,
                    "result_hash": f"hash-{chunk_id}",
                    "stats": {"prompt_tokens": 1, "completion_tokens": 1},
                }
            )
    checkpoint = {
        "schema_version": 1,
        "policy_version": "source-backed-candidate-ids-v2",
        "audit_window": {"start_chapter": AUDIT_START, "end_chapter": AUDIT_END},
        "candidate_set_hash": "deadbeef",
        "config": {},
        "status": "in_progress",
        "chunks": chunks,
        "attempt_records": records,
    }
    audits = Path(root) / "output" / "audits"
    audits.mkdir(parents=True, exist_ok=True)
    _write_json(
        audits / f"audit_{AUDIT_START:04d}_{AUDIT_END:04d}.checkpoint.json", checkpoint
    )
    return checkpoint


def _seed_final_audit_state(
    env,
    run_id=GLM_ACE,
    *,
    last_audited=75,
    write_chapter=True,
    checkpoint=True,
    quarantine=True,
    quarantine_reason=None,
    outcome="terminal_failure",
):
    root = _track_root(env, run_id)
    _seed_progress(env, run_id, last_completed=100, last_audited_chapter=last_audited)
    if write_chapter:
        (root / "output" / "chapters").mkdir(parents=True, exist_ok=True)
        (root / "output" / "chapters" / f"{AUDIT_END:04d}.md").write_text(
            "chapter 100 committed translation\n", encoding="utf-8", newline="\n"
        )
    if checkpoint:
        _seed_audit_checkpoint(root)
    if quarantine:
        _seed_quarantine(
            env,
            run_id,
            first_blocked_chapter=prereg.LAST_CHAPTER + 1,
            reason=quarantine_reason or _audit_failure_reason(),
            outcome=outcome,
        )


def _recovery_fake_module(track, run_root, *, seen, fail_audit=False):
    module = _fake_module(track, run_root, audit=False, seen=seen)

    def run_periodic_audit(start_chapter, end_chapter):
        seen.setdefault("audits", []).append((start_chapter, end_chapter))
        root = Path(run_root)
        audits = root / "output" / "audits"
        checkpoint_path = (
            audits / f"audit_{start_chapter:04d}_{end_chapter:04d}.checkpoint.json"
        )
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        for chunk in checkpoint["chunks"]:
            if chunk.get("status") in ("split", "completed"):
                continue
            attempt = int(chunk.get("attempts", 0)) + 1
            response = module.call_api_with_retries(
                **_legacy_kwargs(
                    module,
                    f"recover audit {chunk['chunk_id']} {start_chapter}-{end_chapter}",
                    max_tokens=4000,
                )
            )
            module.usage_and_cost(response.usage)
            operation_id = (
                "periodic_glossary_audit_chunk:deadbeef:"
                f"{chunk['chunk_id']}:audit:{attempt}"
            )
            seen.setdefault("operations", []).append(operation_id)
            if fail_audit:
                raise RuntimeError("scripted audit content failure")
            checkpoint["attempt_records"].append(
                {
                    "chunk_id": chunk["chunk_id"],
                    "kind": "audit",
                    "attempt": attempt,
                    "operation_id": operation_id,
                    "lifecycle": "response_recorded",
                    "parsed_result": {"decisions": []},
                    "result_hash": "hash",
                    "stats": {},
                }
            )
            chunk["attempts"] = attempt
            chunk["status"] = "completed"
            chunk["result"] = {"decisions": []}
        checkpoint["status"] = "completed"
        checkpoint["combined_result"] = {"decisions": [], "issues": []}
        _write_json(checkpoint_path, checkpoint)
        audit_record = {
            "start_chapter": start_chapter,
            "end_chapter": end_chapter,
            "stats": {},
            "changes": {},
            "issues": [],
        }
        _write_json(
            audits / f"audit_{start_chapter:04d}_{end_chapter:04d}.json", audit_record
        )
        _write_json(root / "data" / "audit_history.json", {"audits": [audit_record]})
        progress = json.loads(
            (root / "data" / "progress.json").read_text(encoding="utf-8")
        )
        progress["last_audited_chapter"] = end_chapter
        _write_json(root / "data" / "progress.json", progress)

    def run_unattended_production(*, target_chapter=None, **_kwargs):
        raise AssertionError("audit recovery must not retranslate any chapter")

    module.run_periodic_audit = run_periodic_audit
    module.run_unattended_production = run_unattended_production
    return module


def _recover(env, run_id=GLM_ACE, *, responder, fail_audit=False, module_loader=None):
    completions = _FakeCompletions(responder)
    factory = _FakeFactory(completions)
    seen = {}
    delays = []

    def loader(*, code_root, track_root_path):
        module = _recovery_fake_module(
            prereg.TRACK_BY_ID[run_id],
            Path(track_root_path),
            seen=seen,
            fail_audit=fail_audit,
        )
        seen["module"] = module
        return {"module": module}

    report = runner.recover_g11_pending_audit(
        execution_root=env["execution"],
        run_id=run_id,
        api_key=API_KEY,
        client_factory=factory,
        production_module_loader=module_loader or loader,
        sleep=lambda seconds: delays.append(seconds),
    )
    return report, completions, factory, seen, delays


def _audit_ok(kwargs):
    return _response(kwargs, content='{"decisions": []}')


def _recovery_cycle_dir(env, run_id, cycle=1):
    return (
        runner.audit_content_recovery_directory(
            _track_root(env, run_id), AUDIT_START, AUDIT_END
        )
        / f"cycle_{cycle:02d}"
    )


def test_audit_only_quarantine_is_classified_recoverable(env):
    _seed_final_audit_state(env)
    decision = runner.classify_audit_content_recovery(
        _track_root(env, GLM_ACE),
        runner.load_quarantine(_track_root(env, GLM_ACE)),
        expected_run_id=GLM_ACE,
        evidence_checkpoint_exists=False,
    )
    assert decision["recoverable"] is True
    assert decision["start_chapter"] == AUDIT_START
    assert decision["end_chapter"] == AUDIT_END
    assert decision["failed_chunks"] == ["c0001"]
    assert decision["already_complete"] is False



def test_terminal_failed_audit_chunk_is_recoverable(env):
    _seed_final_audit_state(env)
    root = _track_root(env, GLM_ACE)
    checkpoint_path = runner.g11_audit_checkpoint_path(root, AUDIT_START, AUDIT_END)
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    failed = next(chunk for chunk in checkpoint["chunks"] if chunk["chunk_id"] == "c0001")
    failed["status"] = "terminal_failed"
    failed["last_error"] = "minimum chunk retry limit reached"
    _write_json(checkpoint_path, checkpoint)

    decision = runner.classify_audit_content_recovery(
        root,
        runner.load_quarantine(root),
        expected_run_id=GLM_ACE,
        evidence_checkpoint_exists=False,
    )
    assert decision["recoverable"] is True
    assert decision["failed_chunks"] == ["c0001"]

    reset = runner.reset_failed_audit_chunks(checkpoint)
    assert reset["reset_chunks"][0]["previous_status"] == "terminal_failed"
    assert failed["status"] == "pending"


def test_chapter_content_quarantine_is_not_audit_recovery(env):
    _seed_progress(env, GLM_ACE, last_completed=1)
    _seed_quarantine(env, GLM_ACE, first_blocked_chapter=1, reason="SystemExit: 1")
    decision = runner.classify_audit_content_recovery(
        _track_root(env, GLM_ACE),
        runner.load_quarantine(_track_root(env, GLM_ACE)),
        expected_run_id=GLM_ACE,
        evidence_checkpoint_exists=False,
    )
    assert decision["recoverable"] is False
    assert decision["reason"] == "reason_not_audit_chunk_failure"


def test_ambiguous_audit_quarantine_is_never_reopened(env):
    _seed_final_audit_state(env, outcome="ambiguous_remote_outcome")
    decision = runner.classify_audit_content_recovery(
        _track_root(env, GLM_ACE),
        runner.load_quarantine(_track_root(env, GLM_ACE)),
        expected_run_id=GLM_ACE,
        evidence_checkpoint_exists=False,
    )
    assert decision["recoverable"] is False
    assert decision["reason"] == "outcome_not_terminal_failure"


def test_started_or_uncertain_journal_blocks_audit_recovery(env):
    _seed_final_audit_state(env)
    _seed_uncertain_journal(
        env, GLM_ACE, AUDIT_END, operation_kind="g11_periodic_audit_deadbeef"
    )
    report, completions, _factory, _seen, _delays = _recover(
        env, responder=_audit_ok
    )
    assert report["status"] == "AUDIT_RECOVERY_NOT_APPLICABLE"
    assert report["decision"]["reason"] == "unresolved_provider_journals"
    assert completions.calls == []
    assert runner.load_quarantine(_track_root(env, GLM_ACE)) is not None


def test_audit_evidence_is_archived_byte_for_byte(env):
    _seed_final_audit_state(env)
    _seed_translation_journal(
        env, GLM_ACE, AUDIT_END, operation_kind="g11_periodic_audit_badc0de"
    )
    root = _track_root(env, GLM_ACE)
    quarantine_before = (root / "data" / "g11_quarantine.json").read_bytes()
    checkpoint_before = runner.g11_audit_checkpoint_path(
        root, AUDIT_START, AUDIT_END
    ).read_bytes()
    journal_dir = root / "data" / "provider_request_journal"
    journal_before = (
        journal_dir / f"{AUDIT_END:04d}.g11_periodic_audit_badc0de.json"
    ).read_bytes()

    report, _completions, _factory, _seen, _delays = _recover(
        env, responder=_audit_ok
    )
    assert report["status"] == "AUDIT_RECOVERED"

    cycle = _recovery_cycle_dir(env, GLM_ACE)
    assert (cycle / "quarantine.json").read_bytes() == quarantine_before
    archived_checkpoint = cycle / (
        f"audit_{AUDIT_START:04d}_{AUDIT_END:04d}.checkpoint.json"
    )
    assert archived_checkpoint.read_bytes() == checkpoint_before
    archived_journal = (
        cycle / "provider_journal" / f"{AUDIT_END:04d}.g11_periodic_audit_badc0de.json"
    )
    assert archived_journal.read_bytes() == journal_before

    manifest = json.loads((cycle / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["archived"]["quarantine"]["sha256"] == hashlib.sha256(
        quarantine_before
    ).hexdigest()
    assert manifest["archived"]["checkpoint"]["sha256"] == hashlib.sha256(
        checkpoint_before
    ).hexdigest()
    assert manifest["archived"]["provider_journals"][0]["sha256"] == hashlib.sha256(
        journal_before
    ).hexdigest()
    # The rejected audit journal is moved out of the active slot so the engine
    # cannot replay the same bad response.
    assert not (
        journal_dir / f"{AUDIT_END:04d}.g11_periodic_audit_badc0de.json"
    ).exists()


def test_completed_audit_chunks_are_preserved_and_only_failed_chunk_reset(env):
    _seed_final_audit_state(env)
    report, _completions, _factory, seen, _delays = _recover(env, responder=_audit_ok)
    assert report["status"] == "AUDIT_RECOVERED"

    reset = report["reset"]
    assert [item["chunk_id"] for item in reset["reset_chunks"]] == ["c0001"]
    assert reset["reset_chunks"][0]["previous_status"] == "validation_failed"
    assert set(reset["preserved_completed_chunks"]) == {
        "c0002",
        "c0003",
        "c0004",
        "c0005",
    }
    # The failed attempt was removed from the active checkpoint and the fresh
    # attempt carries a new operation identity.
    assert reset["removed_attempt_operations"] == [
        "periodic_glossary_audit_chunk:deadbeef:c0001:audit:1"
    ]
    active = json.loads(
        runner.g11_audit_checkpoint_path(
            _track_root(env, GLM_ACE), AUDIT_START, AUDIT_END
        ).read_text(encoding="utf-8")
    )
    by_id = {chunk["chunk_id"]: chunk for chunk in active["chunks"]}
    assert by_id["c0001"]["status"] == "completed"
    assert by_id["c0001"]["attempts"] == 2
    for chunk_id in ("c0002", "c0003", "c0004", "c0005"):
        assert by_id[chunk_id]["status"] == "completed"
        assert by_id[chunk_id]["attempts"] == 1
        assert by_id[chunk_id]["result"]["decisions"][0]["action"] == "keep"
    assert seen["operations"] == [
        "periodic_glossary_audit_chunk:deadbeef:c0001:audit:2"
    ]


def test_failed_old_attempt_is_preserved_in_archive(env):
    _seed_final_audit_state(env)
    report, _completions, _factory, _seen, _delays = _recover(
        env, responder=_audit_ok
    )
    assert report["status"] == "AUDIT_RECOVERED"
    cycle = _recovery_cycle_dir(env, GLM_ACE)
    archived = json.loads(
        (
            cycle / f"audit_{AUDIT_START:04d}_{AUDIT_END:04d}.checkpoint.json"
        ).read_text(encoding="utf-8")
    )
    failed_records = [
        record
        for record in archived["attempt_records"]
        if record["chunk_id"] == "c0001" and record["attempt"] == 1
    ]
    assert len(failed_records) == 1
    assert failed_records[0]["lifecycle"] == "validation_failed"
    assert failed_records[0]["validation_errors"][0]["code"] == "bad_field"
    archived_reset = report["reset"]
    assert archived_reset["removed_attempt_operations"] == [
        "periodic_glossary_audit_chunk:deadbeef:c0001:audit:1"
    ]
    # Completed chunks' attempt evidence stays in the archive untouched.
    assert any(
        record["chunk_id"] == "c0002" for record in archived["attempt_records"]
    )


def test_fresh_audit_attempt_gets_new_operation_identity(env):
    _seed_final_audit_state(env)
    _report, completions, _factory, seen, _delays = _recover(
        env, responder=_audit_ok
    )
    assert completions.calls, "the fresh audit must reach the provider"
    assert seen["operations"] == [
        "periodic_glossary_audit_chunk:deadbeef:c0001:audit:2"
    ]
    assert (
        "periodic_glossary_audit_chunk:deadbeef:c0001:audit:1"
        not in seen["operations"]
    )


def test_max_two_audit_content_recovery_cycles(env):
    _seed_final_audit_state(env)
    first, _c1, _f1, _s1, _d1 = _recover(env, responder=_audit_ok, fail_audit=True)
    assert first["status"] == "AUDIT_RECOVERY_FAILED"
    assert first["cycles_used"] == 1
    second, _c2, _f2, _s2, _d2 = _recover(env, responder=_audit_ok, fail_audit=True)
    assert second["status"] == "AUDIT_RECOVERY_FAILED"
    assert second["cycles_used"] == 2
    third, completions, _f3, _s3, _d3 = _recover(env, responder=_audit_ok)
    assert third["status"] == "AUDIT_RECOVERY_EXHAUSTED"
    assert third["cycles_used"] == 2
    assert completions.calls == []
    assert _recovery_cycle_dir(env, GLM_ACE, 1).is_dir()
    assert _recovery_cycle_dir(env, GLM_ACE, 2).is_dir()
    assert not _recovery_cycle_dir(env, GLM_ACE, 3).exists()


def test_chapter_100_is_not_retranslated(env):
    _seed_final_audit_state(env)
    chapter = (
        _track_root(env, GLM_ACE) / "output" / "chapters" / f"{AUDIT_END:04d}.md"
    )
    before = chapter.read_bytes()
    report, completions, _factory, seen, _delays = _recover(
        env, responder=_audit_ok
    )
    assert report["status"] == "AUDIT_RECOVERED"
    assert report["chapter_retranslation"] is False
    assert "chapters" not in seen
    assert chapter.read_bytes() == before
    assert all(
        "translation" not in kind for kind in _journal_kinds(env, GLM_ACE)
    )
    assert report["provider_calls"] == 1


def test_successful_audit_moves_last_audited_75_to_100(env):
    _seed_final_audit_state(env)
    report, _completions, _factory, _seen, _delays = _recover(
        env, responder=_audit_ok
    )
    assert report["status"] == "AUDIT_RECOVERED"
    assert report["last_completed_chapter"] == 100
    assert report["next_chapter"] == 101
    assert report["last_audited_chapter"] == 100
    progress = json.loads(
        (_track_root(env, GLM_ACE) / "data" / "progress.json").read_text(
            encoding="utf-8"
        )
    )
    assert progress["last_completed"] == 100
    assert progress["next_chapter"] == 101
    assert progress["last_audited_chapter"] == 100


def test_successful_recovery_freezes_checkpoint_100(env):
    _seed_final_audit_state(env)
    evidence = (
        env["execution"] / "g11_evidence" / GLM_ACE / f"{AUDIT_END:04d}"
    )
    assert not evidence.exists()
    report, _completions, _factory, _seen, _delays = _recover(
        env, responder=_audit_ok
    )
    assert report["status"] == "AUDIT_RECOVERED"
    assert evidence.is_dir()
    assert (evidence / "checkpoint.json").is_file()
    assert report["checkpoint_frozen"]["frozen"] is True


def test_already_frozen_checkpoint_100_is_not_refrozen(env):
    _seed_final_audit_state(env, last_audited=AUDIT_END)
    runner.freeze_checkpoints_for_chapter(
        execution_root=env["execution"], run_id=GLM_ACE, chapter=AUDIT_END
    )
    evidence = (
        env["execution"] / "g11_evidence" / GLM_ACE / f"{AUDIT_END:04d}"
    )
    report, completions, _factory, _seen, _delays = _recover(
        env, responder=_audit_ok
    )
    assert report["status"] == "AUDIT_RECOVERED"
    assert report["checkpoint_frozen"]["frozen"] is False
    assert completions.calls == []
    assert evidence.is_dir()


def test_existing_glm_ledger_is_preserved(env):
    responder = lambda kwargs: _response(kwargs, content="ok")
    _execute(env, GLM_ACE, responder=responder, max_chapters=1)
    ledger = _ledger(env, GLM_LEDGER)
    before_reservations = copy.deepcopy(ledger.document["reservations"])
    before_attempts = copy.deepcopy(ledger.document["attempts"])
    before_committed = ledger.committed_credit_equivalent()
    assert before_reservations and before_committed > 0

    _seed_final_audit_state(env)
    report, _completions, _factory, _seen, _delays = _recover(
        env, responder=_audit_ok
    )
    assert report["status"] == "AUDIT_RECOVERED"
    after = _ledger(env, GLM_LEDGER)
    for record in before_reservations:
        assert record in after.document["reservations"]
    for record in before_attempts:
        assert record in after.document["attempts"]
    assert after.committed_credit_equivalent() > before_committed


def test_other_three_tracks_are_untouched(env):
    _seed_final_audit_state(env)
    others = [
        track["run_id"] for track in prereg.TRACKS if track["run_id"] != GLM_ACE
    ]
    before = {run_id: _tree_snapshot(_track_root(env, run_id)) for run_id in others}
    report, _completions, _factory, _seen, _delays = _recover(
        env, responder=_audit_ok
    )
    assert report["status"] == "AUDIT_RECOVERED"
    for run_id in others:
        assert _tree_snapshot(_track_root(env, run_id)) == before[run_id]


def test_already_complete_state_is_idempotent_and_zero_provider(env):
    _seed_final_audit_state(env)
    first, _c1, _f1, _s1, _d1 = _recover(env, responder=_audit_ok)
    assert first["status"] == "AUDIT_RECOVERED"
    second, completions, _factory, _seen, _delays = _recover(
        env, responder=_audit_ok
    )
    assert second["status"] == "NOT_QUARANTINED"
    assert second["provider_calls"] == 0
    assert completions.calls == []


def test_audit_recovery_reads_no_credential_environment(env, monkeypatch):
    _seed_final_audit_state(env)
    observed = []
    real_getenv = os.getenv

    def spy(name, default=None):
        observed.append(name)
        if name in CREDENTIAL_ENV_NAMES:
            raise AssertionError(f"audit recovery must not read {name!r}")
        return real_getenv(name, default)

    monkeypatch.setattr(os, "getenv", spy)
    report, _completions, _factory, _seen, _delays = _recover(
        env, responder=_audit_ok
    )
    assert report["status"] == "AUDIT_RECOVERED"
    assert not [name for name in observed if name in CREDENTIAL_ENV_NAMES]


def test_audit_recovery_makes_zero_network_calls(env, monkeypatch):
    _seed_final_audit_state(env)
    monkeypatch.setattr(socket, "socket", _forbid)
    monkeypatch.setattr(socket, "create_connection", _forbid)
    monkeypatch.setattr(socket, "getaddrinfo", _forbid)
    report, _completions, _factory, _seen, _delays = _recover(
        env, responder=_audit_ok
    )
    assert report["status"] == "AUDIT_RECOVERED"
