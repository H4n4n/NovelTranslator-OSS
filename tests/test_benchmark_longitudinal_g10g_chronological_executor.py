"""Zero-provider tests for the G10G chronological paid executor (arm G10B).

Every Provider interaction goes through an injected fake client, and the
production lifecycle is a scripted fake module injected through the executor's
``production_module_loader`` seam. The scripted lifecycle still goes through
:func:`install_g10g_paid_runtime`, the durable paid-request journal, the
request-level CE reservation, and accepted-usage settlement exactly like the real
engine. No test reads a credential, constructs a real client, executes a
production chapter, or mutates a frozen/external root.
"""

import copy
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import benchmark_longitudinal_g10_chronological as chronological
import benchmark_longitudinal_g10_paid_authorization as paid
import benchmark_longitudinal_g10g_chronological_executor as g10g
from benchmark_longitudinal_g10_chronological import (
    G10B1_LAST_CHAPTER,
    TRACK_BY_ID,
    TRACK_SPECS,
)
from benchmark_longitudinal_g10_preregistration import (
    GLM_MODEL_ID,
    QWEN_MODEL_ID,
)
from benchmark_longitudinal_g10_safety import CreditReservationLedger
from benchmark_longitudinal_g7c_stage1_integration import CONTEXT_ENTRYPOINTS


REPO_ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 30, 3, 0, tzinfo=timezone.utc)
API_KEY = "unit-test-injected-key"
# Marker used by the scripted audit chunks and by the ambiguity responder.
AUDIT_MARKER = "G10B SCRIPTED AUDIT CHUNK"


def _clock():
    return NOW


def _decimal_text(value):
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def _usage(prompt=100, completion=25, cached=0):
    return SimpleNamespace(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
        prompt_tokens_details=SimpleNamespace(cached_tokens=cached),
    )


def _fake_response(kwargs):
    return SimpleNamespace(
        id="fake-response",
        model=kwargs["model"],
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content="scripted lifecycle output"),
            )
        ],
        usage=_usage(),
    )


def _exploding_responder(_kwargs):
    raise AssertionError(
        "no provider transport may occur while a local replay is authorized"
    )


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_progress(run_root, last_completed, *, last_audited=None):
    if last_audited is None:
        last_audited = 25 if last_completed >= 25 else 0
    _write_json(
        run_root / "data" / "progress.json",
        {
            "last_completed": last_completed,
            "next_chapter": last_completed + 1,
            "last_audited_chapter": last_audited,
        },
    )


def _write_run_identity(track, run_root):
    identity = {
        "run_id": track.run_id,
        "run_index": track.run_index,
        "model_id": track.model_id,
        "novel_id": track.novel_id,
        "profile_id": track.profile_id,
        "relative_root": f"runs/{track.run_id}",
        "first_chapter": 1,
        "last_chapter": G10B1_LAST_CHAPTER,
        "audit_checkpoints": [25, 50],
        "audit_interval": 25,
        "state": "s0_unexecuted",
        "provider_calls": 0,
        "paid_execution_authorized": False,
    }
    identity["identity_sha256"] = g10g._sha256_json(identity)
    _write_json(run_root / g10g.G10S0_RUN_IDENTITY_FILENAME, identity)
    return identity["identity_sha256"]


def _write_s0_manifest(s0_root, identities):
    _write_json(
        s0_root / "g10_s0_roots_manifest.json",
        {
            "benchmark_id": "g10-qwen-glm-headtohead-v1",
            "run_count": 4,
            "provider_calls": 0,
            "paid_execution_authorized": False,
            "runs": [
                {
                    "run_id": run_id,
                    "relative_root": f"runs/{run_id}",
                    "model_id": TRACK_BY_ID[run_id].model_id,
                    "novel_id": TRACK_BY_ID[run_id].novel_id,
                    "run_identity_sha256": identity_sha256,
                }
                for run_id, identity_sha256 in sorted(identities.items())
            ],
        },
    )


def _binding(tmp_path, *, matched, s0, freeze):
    value = {
        "schema_version": paid.G10PAID_SCHEMA_VERSION,
        "revision": paid.G10PAID_REVISION,
        "benchmark_id": "g10-qwen-glm-headtohead-v1",
        "g10_paid_authorization_sha256": paid.G10PAID_AUTHORIZATION_SHA256,
        "freeze_id": paid.EXPECTED_FREEZE_ID,
        "freeze_file_sha256": paid.EXPECTED_FREEZE_FILE_SHA256,
        "freeze_file_path": str(freeze / "freeze.json"),
        "freeze_root": str(freeze),
        "budget_ledger_sha256": paid.EXPECTED_LEDGER_SHA256,
        "budget_ledger_path": str(freeze / "g10_budget.json"),
        "maximum_credit_equivalent": "30",
        "arm_credit_equivalent_ceilings": {"G10A": "5", "G10B": "25"},
        "matched_plan_sha256": paid.EXPECTED_MATCHED_PLAN_SHA256,
        "s0_manifest_sha256": paid.EXPECTED_S0_MANIFEST_SHA256,
        "g10a1_safety_sha256": paid.EXPECTED_G10A1_SAFETY_SHA256,
        "pricing_version": paid.EXPECTED_PRICING_VERSION,
        "model_ratecard": copy.deepcopy(paid.MODEL_RATECARD),
        "read_only_production_root": str(tmp_path / "production"),
        "matched_evidence_root": str(matched),
        "s0_root": str(s0),
        "repo_root": str(tmp_path / "production"),
        "s0_runs": {
            run_id: {
                "relative_root": f"runs/{run_id}",
                "model_id": track.model_id,
                "novel_id": track.novel_id,
                "run_identity_sha256": "a" * 64,
            }
            for run_id, track in TRACK_BY_ID.items()
        },
    }
    return value


def _sandbox_ledger(tmp_path):
    path = tmp_path / "freeze" / "g10_budget.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return CreditReservationLedger.create(
        path=path,
        authorization_sha256=paid.EXPECTED_FREEZE_ID,
        maximum_credit_equivalent="30",
        arm_credit_equivalent_ceilings={"G10A": "5", "G10B": "25"},
        clock=_clock,
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    execution = tmp_path / "g10b_execution"
    (execution / "runs").mkdir(parents=True)
    identities = {}
    for track in TRACK_SPECS:
        run_root = execution / "runs" / track.run_id
        (run_root / "data").mkdir(parents=True)
        identities[track.run_id] = _write_run_identity(track, run_root)
        _write_progress(run_root, 0)
    matched = tmp_path / "matched_evidence"
    matched.mkdir()
    s0 = tmp_path / "s0"
    s0.mkdir()
    _write_s0_manifest(s0, identities)
    freeze = tmp_path / "freeze"
    freeze.mkdir()
    binding = _binding(tmp_path, matched=matched, s0=s0, freeze=freeze)
    monkeypatch.setattr(
        g10g, "frozen_execution_binding", lambda **_kwargs: copy.deepcopy(binding)
    )
    ledger = _sandbox_ledger(tmp_path)
    env = {
        "tmp_path": tmp_path,
        "exec": execution,
        "matched": matched,
        "s0": s0,
        "freeze": freeze,
        "binding": binding,
        "ledger": ledger,
        "lifecycle_calls": [],
        "lifecycle_kwargs": [],
        "audit_calls": [],
    }

    # ``execute_g10g_invocation`` opens the ledger its frozen binding names.
    # Re-open that exact path here and hand back the live instance so every
    # assertion observes the durable ledger the executor actually mutates.
    def _open_frozen_budget_ledger(binding, **_kwargs):
        assert Path(binding["budget_ledger_path"]) == ledger.path
        current = CreditReservationLedger.open(
            path=ledger.path,
            authorization_sha256=paid.EXPECTED_FREEZE_ID,
            clock=_clock,
        )
        env["ledger"] = current
        return current

    monkeypatch.setattr(
        g10g, "open_frozen_budget_ledger", _open_frozen_budget_ledger
    )
    return env


def _track_root(env, run_id):
    return env["exec"] / "runs" / run_id


def _plan(env, *, run_id=None, target_chapter, maximum="0.5"):
    track = TRACK_BY_ID[run_id or TRACK_SPECS[0].run_id]
    return g10g.build_g10g_invocation(
        binding=env["binding"],
        production_root=env["exec"],
        run_id=track.run_id,
        target_chapter=target_chapter,
        target_state_fingerprint_sha256=g10g.g10g_target_state_fingerprint(
            _track_root(env, track.run_id)
        ),
        maximum_credit_equivalent=maximum,
    )


def _legacy_kwargs(module, *, content, max_tokens=8000):
    return {
        "model": module.MODEL,
        "messages": [
            {"role": "system", "content": "system::g10b-scripted-lifecycle"},
            {"role": "user", "content": content},
        ],
        "max_tokens": max_tokens,
        "extra_body": {"thinking": {"type": "disabled"}},
    }


def _fake_module(env, run_root, *, audit_chunks, state_extraction_error):
    """Build one scripted lifecycle module with the real patched vocabulary."""
    track = TRACK_BY_ID[run_root.name]
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
        ACTIVE_NOVEL_CONFIG=SimpleNamespace(id=track.novel_id),
        ACTIVE_PROFILE=SimpleNamespace(id=track.profile_id),
        PAID_REQUEST_JOURNAL_DIR=run_root / "data" / "provider_request_journal",
    )
    for name in CONTEXT_ENTRYPOINTS:
        setattr(module, name, lambda *a, **k: None)

    def run_periodic_audit(start_chapter, end_chapter):
        env["audit_calls"].append((start_chapter, end_chapter))
        for index in range(audit_chunks):
            response = module.call_api_with_retries(
                **_legacy_kwargs(
                    module,
                    content=(
                        f"{AUDIT_MARKER} {index + 1} of {audit_chunks} "
                        f"chapters {start_chapter}-{end_chapter}"
                    ),
                    max_tokens=4000,
                )
            )
            module.usage_and_cost(response.usage)

    def run_unattended_production(*, target_chapter=None, **_kwargs):
        chapter = int(target_chapter)
        env["lifecycle_calls"].append(chapter)
        env["lifecycle_kwargs"].append({"target_chapter": chapter})
        module.journaled_chat_request(
            "chapter_translation",
            chapter,
            f"chinese source for chapter {chapter}",
            _legacy_kwargs(module, content=f"translate chapter {chapter}"),
        )
        if state_extraction_error is not None:
            raise state_extraction_error
        module.journaled_chat_request(
            "state_extraction",
            chapter,
            f"chinese source for chapter {chapter}",
            _legacy_kwargs(module, content=f"extract state for chapter {chapter}"),
            translation_text=f"translation for chapter {chapter}",
        )
        if chapter in (25, 50):
            # The production lifecycle owns the audit trigger; the audit window
            # is always the real one for that boundary.
            window = chronological.audit_window_for_target(chapter)
            module.run_periodic_audit(*window)
        _write_progress(run_root, chapter)

    module.run_periodic_audit = run_periodic_audit
    module.run_unattended_production = run_unattended_production
    return module


def _loader(env, *, audit_chunks=1, state_extraction_error=None):
    def load(*, code_root, track_root):
        return {
            "module": _fake_module(
                env,
                Path(track_root),
                audit_chunks=audit_chunks,
                state_extraction_error=state_extraction_error,
            )
        }

    return load


class _FakeCompletions:
    def __init__(self, responder, probe=None):
        self.calls = []
        self.probe_states = []
        self._responder = responder or _fake_response
        self._probe = probe

    def create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        if self._probe is not None:
            self.probe_states.append(self._probe())
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


def _runtime(env, *, responder=None, loader=None, probe=None):
    completions = _FakeCompletions(responder, probe=probe)
    factory = _FakeFactory(completions)

    def call(plan, *, operator_authorized=True, api_key=API_KEY):
        return g10g.execute_g10g_invocation(
            code_root=REPO_ROOT,
            production_root=env["exec"],
            freeze_root=env["freeze"],
            matched_evidence_root=env["matched"],
            s0_root=env["s0"],
            plan=plan,
            expected_plan_sha256=plan["plan_sha256"],
            expected_approval_token=plan["approval_token"],
            operator_authorized=operator_authorized,
            api_key=api_key,
            client_factory=factory,
            production_module_loader=loader,
            clock=_clock,
        )

    return call, completions, factory


def _execute(env, plan, *, responder=None, loader=None, probe=None):
    call, completions, factory = _runtime(
        env, responder=responder, loader=loader, probe=probe
    )
    return call(plan), completions, factory


def _ledger_probe(env):
    """Observe durable ledger state at the instant transport is entered."""

    def probe():
        ledger = CreditReservationLedger.open(
            path=env["ledger"].path,
            authorization_sha256=paid.EXPECTED_FREEZE_ID,
            clock=_clock,
        )
        records = ledger.document["records"]
        return {
            "count": len(records),
            "send_started": [record["send_started"] for record in records],
            "statuses": [record["status"] for record in records],
        }

    return probe


def _journal_dir(run_root):
    return Path(run_root) / "data" / "provider_request_journal"


def _journal_paths(run_root):
    directory = _journal_dir(run_root)
    if not directory.is_dir():
        return []
    return sorted(directory.glob("*.json"), key=lambda item: item.name)


def _journal_kind(path):
    return Path(path).name.split(".")[1]


def _journal_records(run_root):
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in _journal_paths(run_root)
    ]


def _tree_snapshot(root):
    root = Path(root)
    return sorted(
        (path.relative_to(root).as_posix(), path.read_bytes())
        for path in root.rglob("*")
        if path.is_file()
    )


def _progress(run_root):
    return json.loads(
        (Path(run_root) / "data" / "progress.json").read_text(encoding="utf-8")
    )


# ---------------------------------------------------------------------------


def test_g10g_executor_identity_is_zero_provider_and_bounded():
    identity = g10g.g10g_executor_identity()
    assert identity["provider_calls"] == 0
    assert identity["paid_execution_authorized"] is False
    assert identity["arm_id"] == "G10B"
    scope = identity["execution_scope"]
    assert scope["one_track_per_invocation"] is True
    assert scope["one_target_chapter_per_invocation"] is True
    assert scope["single_lifecycle_invocation"] is True
    assert scope["lifecycle_entrypoint"] == "run_unattended_production"
    assert scope["automatic_chapter_advance"] is False
    assert scope["automatic_loop"] is False
    assert scope["automatic_retry"] is False
    assert scope["provider_client_max_retries"] == 0
    assert scope["frozen_s0_root_is_never_an_execution_root"] is True
    accounting = identity["subrequest_accounting"]
    assert accounting["request_level_reservation_before_transport"] is True
    assert accounting["send_started_persisted_before_transport"] is True
    assert accounting["one_application_visible_transport_attempt"] is True
    assert accounting["preregistered_audit_count_forbidden"] is True
    assert identity["quarantine"]["scope_kind"] == (
        "chronological_model_novel_track"
    )
    assert identity["quarantine"]["first_blocked_chapter_required"] is True
    assert identity["replay"]["poisoned_transport_required"] is True
    assert g10g.G10G_EXECUTOR_SHA256 == g10g._sha256_json(identity)


def test_executor_rebuilds_frozen_binding_from_authoritative_code_root(
    env, monkeypatch
):
    """The mutable G10B execution root must never stand in for frozen sources."""
    captured = {}
    frozen_binding = copy.deepcopy(env["binding"])

    def capture_binding(**kwargs):
        captured.update(kwargs)
        return copy.deepcopy(frozen_binding)

    monkeypatch.setattr(g10g, "frozen_execution_binding", capture_binding)

    track = TRACK_SPECS[0]
    plan = _plan(env, run_id=track.run_id, target_chapter=1)
    report = g10g.execute_g10g_invocation(
        code_root=REPO_ROOT,
        production_root=env["exec"],
        freeze_root=env["freeze"],
        matched_evidence_root=env["matched"],
        s0_root=env["s0"],
        plan=plan,
        expected_plan_sha256=plan["plan_sha256"],
        expected_approval_token=plan["approval_token"],
        operator_authorized=False,
        api_key=None,
    )

    authoritative = REPO_ROOT.resolve()
    execution = Path(env["exec"]).resolve()
    assert Path(captured["production_root"]).resolve() == authoritative
    assert Path(captured["repo_root"]).resolve() == authoritative
    assert Path(captured["production_root"]).resolve() != execution
    assert report["status"] == "AUTHORIZATION_REQUIRED"
    assert report["provider_calls"] == 0
    assert report["provider_transport_attempts"] == 0


def test_real_lifecycle_runs_inside_exact_track_working_directory(env):
    """Relative production paths must stay isolated inside one writable track."""
    track = TRACK_SPECS[0]
    run_root = _track_root(env, track.run_id).resolve()
    plan = _plan(env, run_id=track.run_id, target_chapter=1)

    base_loader = _loader(env)
    observed = []

    def checked_loader(*, code_root, track_root):
        loaded = base_loader(code_root=code_root, track_root=track_root)
        module = loaded["module"]
        original = module.run_unattended_production

        def checked_runner(*args, **kwargs):
            observed.append(Path.cwd().resolve())
            if Path.cwd().resolve() != Path(track_root).resolve():
                raise AssertionError(
                    "real lifecycle escaped the exact G10B track working directory"
                )
            return original(*args, **kwargs)

        module.run_unattended_production = checked_runner
        return loaded

    report, _completions, _factory = _execute(
        env,
        plan,
        loader=checked_loader,
    )

    assert report["status"] == "COMPLETED", report
    assert observed == [run_root]
    assert _progress(run_root)["last_completed"] == 1


def test_four_track_enumeration_and_unknown_run_id_rejected(env):
    assert len(TRACK_SPECS) == 4
    assert len(TRACK_BY_ID) == 4
    assert [track.run_index for track in TRACK_SPECS] == [1, 2, 3, 4]
    assert [track.run_id for track in TRACK_SPECS] == [
        track.run_id for track in sorted(TRACK_SPECS, key=lambda item: item.run_index)
    ]
    assert {
        (track.model_id, track.novel_id) for track in TRACK_SPECS
    } == {
        (QWEN_MODEL_ID, "ace_evolution"),
        (GLM_MODEL_ID, "ace_evolution"),
        (QWEN_MODEL_ID, "rogue_first_prince"),
        (GLM_MODEL_ID, "rogue_first_prince"),
    }
    for track in TRACK_SPECS:
        assert TRACK_BY_ID[track.run_id] is track
        assert track.prereg_track_id == f"{track.model_id}::{track.novel_id}"
        assert _track_root(env, track.run_id).is_dir()

    with pytest.raises(g10g.G10GExecutionError, match="unknown G10B run id"):
        g10g.resolve_track_root(
            production_root=env["exec"], run_id="g10b-run99-nope"
        )
    with pytest.raises(g10g.G10GExecutionError, match="unknown G10B run id"):
        g10g.build_g10g_invocation(
            binding=env["binding"],
            production_root=env["exec"],
            run_id="g10b-run99-nope",
            target_chapter=1,
            target_state_fingerprint_sha256="a" * 64,
            maximum_credit_equivalent="0.5",
        )


def test_target_chapter_must_be_the_tracks_planned_next_chapter(env):
    track = TRACK_SPECS[0]
    plan = _plan(env, run_id=track.run_id, target_chapter=1)
    assert plan["target_chapter"] == 1
    assert plan["classification"] == {
        "status": "ready_for_target",
        "last_completed": 0,
        "last_audited_chapter": 0,
    }
    with pytest.raises(
        g10g.G10GExecutionError, match="not the track's planned next chapter"
    ):
        _plan(env, run_id=track.run_id, target_chapter=2)
    with pytest.raises(
        g10g.G10GExecutionError, match="not the track's planned next chapter"
    ):
        _plan(env, run_id=track.run_id, target_chapter=25)
    with pytest.raises(g10g.G10GExecutionError, match="within 1..50"):
        _plan(env, run_id=track.run_id, target_chapter=G10B1_LAST_CHAPTER + 1)

    partner = TRACK_SPECS[1]
    partner_plan = _plan(env, run_id=partner.run_id, target_chapter=1)
    assert partner_plan["run_id"] != plan["run_id"]
    assert partner_plan["model_id"] != plan["model_id"]


@pytest.mark.parametrize(
    "name, progress, target, blocked_chapter",
    (
        (
            "corrupt_block",
            {
                "last_completed": 5,
                "next_chapter": 9,
                "last_audited_chapter": 0,
            },
            9,
            None,
        ),
        (
            "audit_recovery_required",
            {
                "last_completed": 25,
                "next_chapter": 26,
                "last_audited_chapter": 0,
            },
            26,
            None,
        ),
        (
            "complete",
            {
                "last_completed": 50,
                "next_chapter": 51,
                "last_audited_chapter": 50,
            },
            50,
            None,
        ),
        (
            "quarantined",
            {
                "last_completed": 24,
                "next_chapter": 25,
                "last_audited_chapter": 0,
            },
            25,
            25,
        ),
    ),
)
def test_non_ready_track_states_are_rejected_with_zero_transport(
    env, name, progress, target, blocked_chapter
):
    track = TRACK_SPECS[0]
    run_root = _track_root(env, track.run_id)
    _write_json(run_root / "data" / "progress.json", progress)
    if blocked_chapter is not None:
        paid.quarantine_chronological_track(
            path=g10g.track_quarantine_path(run_root),
            model_id=track.model_id,
            novel_id=track.novel_id,
            run_id=track.run_id,
            first_blocked_chapter=blocked_chapter,
            request_sha256="a" * 64,
            reservation_id="b" * 64,
            reason="fixture durable quarantine",
            clock=_clock,
        )
        classification = chronological.classify_track_progress(
            progress,
            quarantine=paid.load_paid_quarantine(
                g10g.track_quarantine_path(run_root)
            ),
            expected_run_id=track.run_id,
        )
        assert classification["status"] == "quarantined"

    with pytest.raises(g10g.G10GExecutionError, match=name):
        _plan(env, run_id=track.run_id, target_chapter=target)

    call, completions, factory = _runtime(env, loader=_loader(env))
    assert completions.calls == []
    assert factory.kwargs == []
    assert env["lifecycle_calls"] == []
    assert not g10g.invocation_ledger_path(run_root, target).exists()


def test_plan_never_preregisters_a_fixed_audit_count(env):
    plan = _plan(env, run_id=TRACK_SPECS[0].run_id, target_chapter=1)
    g10g.assert_no_preregistered_audit_count(plan)
    assert plan["audit"]["real_production_trigger_only"] is True
    assert plan["audit"]["no_preregistered_request_count"] is True
    assert plan["audit"]["no_synthetic_checkpoints"] is True
    assert "audit_count" not in json.dumps(plan)

    tampered = copy.deepcopy(plan)
    tampered["audit"]["audit_subrequest_count"] = 2
    with pytest.raises(g10g.G10GExecutionError, match="must not preregister"):
        g10g.assert_no_preregistered_audit_count(tampered)

    # The same shape also fails the plan-envelope verification.
    body = copy.deepcopy(tampered)
    body.pop("plan_sha256")
    tampered["plan_sha256"] = g10g._sha256_json(body)
    with pytest.raises(g10g.G10GExecutionError, match="must not preregister"):
        g10g.verify_g10g_invocation(
            plan=tampered,
            binding=env["binding"],
            production_root=env["exec"],
        )


@pytest.mark.parametrize("audit_chunks", (0, 1, 3))
def test_same_plan_is_valid_for_any_emitted_subrequest_count(env, audit_chunks):
    track = TRACK_SPECS[0]
    run_root = _track_root(env, track.run_id)
    _write_progress(run_root, 24)
    plan = _plan(env, run_id=track.run_id, target_chapter=25)
    rebuilt = _plan(env, run_id=track.run_id, target_chapter=25)
    # Nothing in the plan freezes how many audit subrequests a chapter needs:
    # the identical plan admits one, three, or zero real audit sends.
    assert rebuilt == plan
    assert plan["audit"]["due_after_target"] is True
    assert plan["audit"]["window"] == [1, 25]

    report, completions, _factory = _execute(
        env, plan, loader=_loader(env, audit_chunks=audit_chunks)
    )

    expected = 2 + audit_chunks
    assert report["status"] == "COMPLETED"
    assert len(completions.calls) == expected
    assert report["transport_attempts"] == expected
    assert report["provider_calls"] == expected
    assert [row["logical_operation"] for row in report["subrequests"]] == (
        ["translation", "state_extraction"]
        + ["periodic_audit"] * audit_chunks
    )
    assert _progress(run_root)["last_completed"] == 25
    ledger_record = json.loads(
        Path(report["invocation_ledger_path"]).read_text(encoding="utf-8")
    )
    assert ledger_record["observed_subrequest_count"] == expected
    assert ledger_record["provider_calls"] == expected


def test_per_subrequest_reservation_precedes_transport_exactly_once(env):
    track = TRACK_SPECS[0]
    run_root = _track_root(env, track.run_id)
    plan = _plan(env, run_id=track.run_id, target_chapter=1)
    report, completions, _factory = _execute(
        env, plan, probe=_ledger_probe(env), loader=_loader(env)
    )

    assert report["status"] == "COMPLETED", report.get("error")
    assert len(completions.calls) == 2
    # The durable reservation and its send_started marker existed strictly
    # before each transport attempt, and each attempt owned its own reservation.
    # The first subrequest is already settled to ``accepted`` before the second
    # transport begins: accounting is per-subrequest, not batched at the end.
    assert completions.probe_states == [
        {"count": 1, "send_started": [True], "statuses": ["reserved"]},
        {
            "count": 2,
            "send_started": [True, True],
            "statuses": ["accepted", "reserved"],
        },
    ]
    # One application-visible transport attempt per subrequest, over distinct
    # request bodies.
    assert len(
        {json.dumps(call, sort_keys=True) for call in completions.calls}
    ) == 2

    ledger_records = env["ledger"].document["records"]
    assert len(ledger_records) == 2
    assert all(record["arm_id"] == "G10B" for record in ledger_records)
    assert all(record["send_started"] is True for record in ledger_records)
    assert all(record["status"] == "accepted" for record in ledger_records)
    request_shas = [record["request_sha256"] for record in ledger_records]
    assert len(set(request_shas)) == 2

    journals = _journal_records(run_root)
    assert len(journals) == 2
    assert all(record["status"] == "response_recorded" for record in journals)
    assert {record["request_sha256"] for record in journals} == set(request_shas)
    assert [_journal_kind(path) for path in _journal_paths(run_root)] == [
        "g10b_state_extraction",
        "g10b_translation",
    ]
    assert {
        row["request_sha256"] for row in report["subrequests"]
    } == set(request_shas)
    assert {
        row["reservation_id"] for row in report["subrequests"]
    } == {record["reservation_id"] for record in ledger_records}

    # Exact accepted CE from the frozen ratecard and the accepted Provider usage.
    expected_each = paid.accepted_credit_equivalent(track.model_id, _usage())
    assert {
        row["accepted_credit_equivalent"] for row in report["subrequests"]
    } == {_decimal_text(expected_each)}
    assert env["ledger"].accepted_credit_equivalent == expected_each * 2
    assert report["committed_credit_equivalent"] == _decimal_text(
        expected_each * 2
    )
    assert report["remaining_credit_equivalent"] == _decimal_text(
        Decimal("30") - expected_each * 2
    )
    assert report["lifecycle_invocations"] == 1
    assert env["lifecycle_kwargs"] == [{"target_chapter": 1}]


@pytest.mark.parametrize(
    "target, last_completed, last_audited, audit_chunks",
    (
        (25, 24, 0, 0),
        (25, 24, 0, 1),
        (25, 24, 0, 3),
        (50, 49, 25, 2),
    ),
)
def test_real_audit_delegation_matches_emitted_subrequests(
    env, target, last_completed, last_audited, audit_chunks
):
    track = TRACK_SPECS[0]
    run_root = _track_root(env, track.run_id)
    _write_progress(run_root, last_completed, last_audited=last_audited)
    window = chronological.audit_window_for_target(target)
    plan = _plan(env, run_id=track.run_id, target_chapter=target)
    assert plan["audit"]["window"] == list(window)

    report, completions, _factory = _execute(
        env, plan, loader=_loader(env, audit_chunks=audit_chunks)
    )

    expected = 2 + audit_chunks
    assert report["status"] == "COMPLETED"
    assert len(completions.calls) == expected
    assert report["transport_attempts"] == expected
    # The lifecycle drove the audit exactly once, with the real window, and the
    # executor synthesized nothing: the transport count equals what it emitted.
    assert env["audit_calls"] == [window]
    audit_rows = [
        row
        for row in report["subrequests"]
        if row["logical_operation"] == "periodic_audit"
    ]
    assert len(audit_rows) == audit_chunks
    kinds = [_journal_kind(path) for path in _journal_paths(run_root)]
    audit_journals = [
        kind for kind in kinds if kind.startswith("g10b_periodic_audit")
    ]
    assert len(audit_journals) == audit_chunks
    if audit_chunks == 0:
        assert kinds == ["g10b_state_extraction", "g10b_translation"]
    assert len({record["request_sha256"] for record in audit_rows}) == (
        audit_chunks
    )
    assert _progress(run_root)["last_completed"] == target
    assert _progress(run_root)["next_chapter"] == target + 1


def test_ch25_ambiguity_quarantines_only_that_track(env):
    track = TRACK_SPECS[0]
    partner = TRACK_SPECS[1]
    run_root = _track_root(env, track.run_id)
    _write_progress(run_root, 24)
    plan = _plan(env, run_id=track.run_id, target_chapter=25)

    def responder(kwargs):
        if AUDIT_MARKER in json.dumps(kwargs.get("messages"), ensure_ascii=False):
            raise TimeoutError("ambiguous audit timeout")
        return _fake_response(kwargs)

    report, completions, _factory = _execute(
        env, plan, responder=responder, loader=_loader(env, audit_chunks=1)
    )

    assert report["status"] == "QUARANTINED"
    assert report["full_reservation_retained"] is True
    assert report["progress_advanced"] is False
    assert report["first_blocked_chapter"] == 25
    assert report["ambiguity"]["chapter"] == 25
    assert report["ambiguity"]["journal_status"] == "uncertain"
    assert report["provider_transport_attempts"] == 3
    assert len(completions.calls) == 3
    assert report["automatic_chapter_advance"] is False
    assert report["automatic_retry"] is False

    # A durable track-scoped quarantine is written with the first blocked chapter.
    marker = json.loads(
        g10g.track_quarantine_path(run_root).read_text(encoding="utf-8")
    )
    assert marker["scope_kind"] == "chronological_model_novel_track"
    assert marker["scope_id"] == track.run_id
    assert marker["first_blocked_chapter"] == 25
    assert marker["outcome"] == "ambiguous_remote_outcome"
    # The frozen quarantine marker schema binds the model/novel track through
    # ``scope_id`` (the run identity) rather than a separate model_id field.
    assert marker["scope_id"].endswith(track.novel_id)

    # The chapter never advanced, and no unresolved subrequest was reissued.
    assert _progress(run_root)["last_completed"] == 24
    assert _progress(run_root)["next_chapter"] == 25
    assert not g10g.invocation_ledger_path(run_root, 25).exists()
    uncertain = [
        record
        for record in _journal_records(run_root)
        if record["status"] == "uncertain"
    ]
    assert len(uncertain) == 1
    assert uncertain[0]["chapter"] == 25
    assert uncertain[0]["operation_kind"] == "periodic_audit"

    records = env["ledger"].document["records"]
    assert [record["status"] for record in records] == [
        "accepted",
        "accepted",
        "ambiguous",
    ]
    expected_each = paid.accepted_credit_equivalent(track.model_id, _usage())
    assert env["ledger"].committed_credit_equivalent == (
        expected_each * 2 + Decimal("0.5")
    )

    # The same track's next invocation is blocked with zero transport.
    call, completions2, factory2 = _runtime(
        env, loader=_loader(env, audit_chunks=1)
    )
    with pytest.raises(g10g.G10GExecutionError, match="not ready"):
        call(plan)
    assert completions2.calls == []
    assert factory2.kwargs == []
    assert env["lifecycle_calls"] == [25]
    with pytest.raises(g10g.G10GExecutionError, match="quarantined"):
        _plan(env, run_id=track.run_id, target_chapter=25)

    # Other tracks are unaffected and still execute their own chapter.
    other_plan = _plan(env, run_id=partner.run_id, target_chapter=1)
    other_report, other_completions, _factory = _execute(
        env, other_plan, loader=_loader(env, audit_chunks=0)
    )
    assert other_report["status"] == "COMPLETED"
    assert len(other_completions.calls) == 2
    assert other_report["model_id"] == partner.model_id
    assert env["lifecycle_calls"] == [25, 1]
    assert not g10g.track_quarantine_path(
        _track_root(env, partner.run_id)
    ).exists()


def test_terminal_lifecycle_failure_is_terminal_without_retry_or_advance(env):
    track = TRACK_SPECS[0]
    run_root = _track_root(env, track.run_id)
    _write_progress(run_root, 24)
    plan = _plan(env, run_id=track.run_id, target_chapter=25)
    report, completions, factory = _execute(
        env,
        plan,
        loader=_loader(
            env, state_extraction_error=RuntimeError("policy refusal")
        ),
    )

    assert report["status"] == "TERMINAL_FAILURE"
    assert report["terminal"] is True
    assert report["error"]["type"] == "RuntimeError"
    assert report["automatic_retry"] is False
    assert report["automatic_chapter_advance"] is False
    assert report["operation_replaced"] is False
    assert report["progress_advanced"] is False
    assert report["provider_transport_attempts"] == 1
    # Exactly one application-visible attempt, no retry, and the no-retry client
    # policy is preserved.
    assert len(completions.calls) == 1
    assert factory.kwargs[0]["max_retries"] == 0
    assert env["lifecycle_calls"] == [25]

    # No silent replacement request: one journal, one accepted reservation.
    journals = _journal_records(run_root)
    assert len(journals) == 1
    assert journals[0]["status"] == "response_recorded"
    assert journals[0]["chapter"] == 25
    records = env["ledger"].document["records"]
    assert len(records) == 1
    assert records[0]["status"] == "accepted"

    # Nothing advanced, and no completed-invocation ledger was written.
    assert _progress(run_root)["last_completed"] == 24
    assert not g10g.invocation_ledger_path(run_root, 25).exists()
    assert not g10g.track_quarantine_path(run_root).exists()


def test_completed_invocation_persists_progress_and_invocation_ledger(env):
    track = TRACK_SPECS[2]
    run_root = _track_root(env, track.run_id)
    _write_progress(run_root, 24)
    plan = _plan(env, run_id=track.run_id, target_chapter=25)
    report, completions, _factory = _execute(
        env, plan, loader=_loader(env, audit_chunks=2)
    )

    assert report["status"] == "COMPLETED"
    assert report["target_chapter"] == 25
    assert report["next_chapter"] == 26
    assert report["last_audited_chapter"] == 25
    assert report["lifecycle_invocations"] == 1
    assert report["automatic_chapter_advance"] is False
    assert report["automatic_retry"] is False
    assert len(completions.calls) == 4
    # ``run_unattended_production`` ran exactly once, for the approved chapter.
    assert env["lifecycle_kwargs"] == [{"target_chapter": 25}]

    progress = _progress(run_root)
    assert progress["last_completed"] == 25
    assert progress["next_chapter"] == 26
    assert Path(report["invocation_ledger_path"]) == (
        g10g.invocation_ledger_path(run_root, 25)
    )
    assert not g10g.invocation_ledger_path(run_root, 26).exists()
    record = json.loads(
        Path(report["invocation_ledger_path"]).read_text(encoding="utf-8")
    )
    assert record["run_id"] == track.run_id
    assert record["lifecycle_invocations"] == 1
    assert record["observed_subrequest_count"] == 4
    assert record["provider_calls"] == 4
    assert record["automatic_chapter_advance"] is False
    assert record["automatic_retry"] is False


def test_response_recorded_replay_is_local_only_with_poisoned_transport(env):
    track = TRACK_SPECS[0]
    run_root = _track_root(env, track.run_id)
    plan = _plan(env, run_id=track.run_id, target_chapter=1)
    first, completions, _factory = _execute(env, plan, loader=_loader(env))
    assert first["status"] == "COMPLETED"
    assert first["new_provider_sends"] == 2
    assert len(completions.calls) == 2
    ledger_bytes = env["ledger"].path.read_bytes()
    journals_before = {
        path.name: path.read_bytes() for path in _journal_paths(run_root)
    }

    # The durable paid responses exist but the lifecycle never committed the
    # chapter: restore the exact pre-invocation target state and re-invoke.
    _write_progress(run_root, 0)
    replay_plan = _plan(env, run_id=track.run_id, target_chapter=1)

    report, replay_completions, _factory2 = _execute(
        env, replay_plan, responder=_exploding_responder, loader=_loader(env)
    )

    assert report["status"] == "COMPLETED"
    assert report["replayed_subrequests"] == 2
    assert report["new_provider_sends"] == 0
    assert report["transport_attempts"] == 0
    assert report["provider_calls"] == 0
    assert replay_completions.calls == []
    assert [row["replayed"] for row in report["subrequests"]] == [True, True]
    for row in report["subrequests"]:
        gate = row["replay_gate"]
        assert gate["replay_authorized"] is True
        assert gate["provider_send_authorized"] is False
        assert gate["transport_poisoned"] is True
        assert gate["journal_status"] == "response_recorded"
        assert gate["ledger_status"] == "accepted"

    # A local replay mutates neither the ledger nor the durable journals.
    assert env["ledger"].path.read_bytes() == ledger_bytes
    assert {
        path.name: path.read_bytes() for path in _journal_paths(run_root)
    } == journals_before
    assert _progress(run_root)["last_completed"] == 1
    assert _progress(run_root)["next_chapter"] == 2


@pytest.mark.parametrize("operator_authorized", (False, None, ""))
def test_missing_or_empty_operator_authorization_requires_authorization(
    env, operator_authorized
):
    track = TRACK_SPECS[0]
    run_root = _track_root(env, track.run_id)
    plan = _plan(env, run_id=track.run_id, target_chapter=1)
    before = _tree_snapshot(run_root)
    ledger_bytes = env["ledger"].path.read_bytes()
    call, completions, factory = _runtime(env, loader=_loader(env))

    report = call(plan, operator_authorized=operator_authorized)

    assert report["status"] == "AUTHORIZATION_REQUIRED"
    assert report["provider_calls"] == 0
    assert report["provider_transport_attempts"] == 0
    assert report["paid_execution_authorized"] is False
    assert report["track_status"] == "ready_for_target"
    assert "AUTHORIZATION_REQUIRED" in report["reason"]
    assert factory.kwargs == []
    assert completions.calls == []
    assert env["lifecycle_calls"] == []
    assert _tree_snapshot(run_root) == before
    assert env["ledger"].path.read_bytes() == ledger_bytes
    assert not g10g.invocation_ledger_path(run_root, 1).exists()
    assert not g10g.track_quarantine_path(run_root).exists()
    assert not _journal_dir(run_root).exists()


def test_missing_api_key_fails_before_any_side_effect(env):
    track = TRACK_SPECS[0]
    run_root = _track_root(env, track.run_id)
    plan = _plan(env, run_id=track.run_id, target_chapter=1)
    before = _tree_snapshot(run_root)
    ledger_bytes = env["ledger"].path.read_bytes()
    call, completions, factory = _runtime(env, loader=_loader(env))

    with pytest.raises(g10g.G10GExecutionError, match="injected API key"):
        call(plan, api_key=None)

    assert factory.kwargs == []
    assert completions.calls == []
    assert env["lifecycle_calls"] == []
    assert _tree_snapshot(run_root) == before
    assert env["ledger"].path.read_bytes() == ledger_bytes
    assert not _journal_dir(run_root).exists()