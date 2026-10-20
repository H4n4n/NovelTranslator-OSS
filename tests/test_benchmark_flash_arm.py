import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import benchmark_flash_arm as arm
from benchmark_novel import BenchmarkSafetyError


HEAD = "a" * 40
PLAN_SHA = "b" * 64


class FakeCompletions:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


class FakeClient:
    def __init__(self, response=None, error=None):
        self.completions = FakeCompletions(response=response, error=error)
        self.chat = SimpleNamespace(completions=self.completions)


def _case(ordinal=1, novel="great_king_zhuxia", chapter=70):
    case_id = f"{ordinal:02d}-{novel}-ch{chapter:04d}"
    return {
        "ordinal": ordinal,
        "case_id": case_id,
        "novel_id": novel,
        "chapter_number": chapter,
        "stratum": 1,
        "role": "representative",
        "sample_plan_sha256": "1" * 64,
        "sample_plan_path": f"C:/plans/{novel}.json",
        "source_sha256": "2" * 64,
        "relevant_glossary_sha256": "3" * 64,
        "frozen_pro_request_sha256": "4" * 64,
        "flash_request_sha256": "5" * 64,
        "transaction_identity_sha256": "6" * 64,
        "case_dir": f"cases/{case_id}",
    }


def _plan(cases):
    return {
        "schema_version": arm.ARM_SCHEMA_VERSION,
        "kind": arm.ARM_KIND,
        "status": "prepared",
        "arm_id": "arm-test-id",
        "identity": {
            "repo_head": HEAD,
            "cases": cases,
        },
        "protected_state_bindings": {},
    }


def _write_complete_evidence(
    root,
    case,
    *,
    response_model=arm.EXPECTED_FLASH_API_MODEL,
    finish_reason="stop",
    identity_overrides=None,
    safety_overrides=None,
):
    case_dir = root / case["case_dir"]
    case_dir.mkdir(parents=True, exist_ok=True)
    requests_dir = root / "requests"
    requests_dir.mkdir(parents=True, exist_ok=True)

    request_bytes = b'{"model":"deepseek-flash","max_tokens":12000}'
    request_sha = arm._sha256_bytes(request_bytes)
    case["flash_request_sha256"] = request_sha
    (case_dir / "request.json").write_bytes(request_bytes)
    (requests_dir / f"{case['case_id']}.json").write_bytes(request_bytes)

    translation_bytes = b"Complete translated chapter."
    translation_sha = arm._sha256_bytes(translation_bytes)
    (case_dir / "translation.txt").write_bytes(translation_bytes)

    protected_sha = "9" * 64
    identity = {
        "kind": "deepseek-v4.1-flash-smoke-v1",
        "repo_head": HEAD,
        "novel_id": case["novel_id"],
        "chapter_number": case["chapter_number"],
        "sample_plan_sha256": case["sample_plan_sha256"],
        "source_sha256": case["source_sha256"],
        "relevant_glossary_sha256": case["relevant_glossary_sha256"],
        "frozen_pro_request_sha256": case["frozen_pro_request_sha256"],
        "flash_request_sha256": request_sha,
        "provider": "deepseek",
        "api_model": arm.EXPECTED_FLASH_API_MODEL,
        "model_version": arm.EXPECTED_FLASH_VERSION,
        "thinking": arm.EXPECTED_FLASH_THINKING,
        "max_tokens": arm.EXPECTED_FLASH_MAX_TOKENS,
        "output_dir": str(case_dir),
        "protected_state_sha256": protected_sha,
    }
    identity.update(identity_overrides or {})
    identity_sha = arm._canonical_json_sha256(identity)
    case["transaction_identity_sha256"] = identity_sha

    safety = {
        "protected_state_before_sha256": protected_sha,
        "protected_state_after_sha256": protected_sha,
        "protected_state_verified_unchanged": True,
        "provider_calls_attempted": 1,
        "paid_requests_attempted": 1,
        "provider_response_recorded": True,
        "automatic_retry_forbidden": True,
    }
    safety.update(safety_overrides or {})
    complete = {
        "status": "complete",
        "identity_sha256": identity_sha,
        "identity": identity,
        "provider_response": {
            "response_model": response_model,
            "system_fingerprint": "fp-test",
            "finish_reason": finish_reason,
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 120,
            },
        },
        "result": {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_cost_usd": 0.001,
            "remaining_cjk_character_count": 0,
        },
        "artifacts": {
            "request_json": "request.json",
            "request_sha256": request_sha,
            "journal": "journal.jsonl",
            "translation": "translation.txt",
            "translation_sha256": translation_sha,
        },
        "safety": safety,
    }
    complete_path = case_dir / "complete.json"
    complete_path.write_text(json.dumps(complete, indent=2), encoding="utf-8")
    events = ["planned", "started", "response_recorded", "complete"]
    (case_dir / "journal.jsonl").write_text(
        "".join(json.dumps({"event": event}) + "\n" for event in events),
        encoding="utf-8",
    )
    return complete_path


def test_arm_status_selects_first_planned_after_imported_smoke(tmp_path):
    imported = _case(1, chapter=63)
    pending = _case(2, chapter=70)
    plan = _plan([imported, pending])
    root = tmp_path / "arm"
    imported_dir = root / imported["case_dir"]
    imported_dir.mkdir(parents=True)
    (imported_dir / "import_record.json").write_text("{}", encoding="utf-8")

    status = arm.arm_status(plan, root)

    assert status["counts"]["imported"] == 1
    assert status["counts"]["planned"] == 1
    assert status["completed_total"] == 1
    assert status["next_case"]["case_id"] == pending["case_id"]
    assert status["blocked"] is False


def test_arm_status_blocks_after_uncertain(tmp_path):
    bad = _case(1)
    pending = _case(2, chapter=71)
    plan = _plan([bad, pending])
    root = tmp_path / "arm"
    bad_dir = root / bad["case_dir"]
    bad_dir.mkdir(parents=True)
    (bad_dir / "uncertain.json").write_text("{}", encoding="utf-8")

    status = arm.arm_status(plan, root)

    assert status["blocked"] is True
    assert status["counts"]["uncertain"] == 1


def test_case_status_accepts_semantically_valid_persisted_complete(tmp_path):
    case = _case()
    plan = _plan([case])
    root = tmp_path / "arm"
    _write_complete_evidence(root, case)

    assert arm._case_status(root, case) == "complete"
    status = arm.arm_status(plan, root)
    assert status["counts"]["complete"] == 1
    assert status["counts"]["invalid_complete"] == 0
    assert status["blocked"] is False
    assert status["completed_total"] == 1


@pytest.mark.parametrize(
    ("response_model", "finish_reason"),
    [
        ("deepseek-v4-pro", "stop"),
        (arm.EXPECTED_FLASH_API_MODEL, "length"),
    ],
)
def test_fresh_status_blocks_invalid_provider_semantics_in_persisted_complete(
    tmp_path, response_model, finish_reason
):
    bad = _case(1)
    pending = _case(2, chapter=71)
    plan = _plan([bad, pending])
    root = tmp_path / "arm"
    complete_path = _write_complete_evidence(
        root,
        bad,
        response_model=response_model,
        finish_reason=finish_reason,
    )
    paid_evidence_before = complete_path.read_bytes()

    status = arm.arm_status(plan, root)

    assert status["counts"]["invalid_complete"] == 1
    assert status["counts"]["complete"] == 0
    assert status["blocked"] is True
    assert status["completed_total"] == 0
    assert status["next_case"]["case_id"] == pending["case_id"]
    assert complete_path.read_bytes() == paid_evidence_before


def test_fresh_status_blocks_invalid_complete_identity_binding(tmp_path):
    bad = _case(1)
    pending = _case(2, chapter=71)
    plan = _plan([bad, pending])
    root = tmp_path / "arm"
    complete_path = _write_complete_evidence(
        root,
        bad,
        identity_overrides={"chapter_number": 999},
    )
    paid_evidence_before = complete_path.read_bytes()

    status = arm.arm_status(plan, root)

    assert status["counts"]["invalid_complete"] == 1
    assert status["blocked"] is True
    assert complete_path.read_bytes() == paid_evidence_before


def test_fresh_status_blocks_invalid_complete_safety_proof(tmp_path):
    bad = _case(1)
    pending = _case(2, chapter=71)
    plan = _plan([bad, pending])
    root = tmp_path / "arm"
    complete_path = _write_complete_evidence(
        root,
        bad,
        safety_overrides={"provider_response_recorded": False},
    )
    paid_evidence_before = complete_path.read_bytes()

    status = arm.arm_status(plan, root)

    assert status["counts"]["invalid_complete"] == 1
    assert status["blocked"] is True
    assert complete_path.read_bytes() == paid_evidence_before


def test_execute_next_never_reissues_after_invalid_complete_on_fresh_process(
    monkeypatch, tmp_path
):
    bad = _case(1)
    pending = _case(2, chapter=71)
    plan = _plan([bad, pending])
    root = tmp_path / "arm"
    complete_path = _write_complete_evidence(
        root,
        bad,
        response_model="deepseek-v4-pro",
    )
    paid_evidence_before = complete_path.read_bytes()
    calls = {"paid": 0, "rebuild": 0}

    monkeypatch.setattr(
        arm, "_load_arm", lambda *args, **kwargs: (plan, root, PLAN_SHA)
    )
    monkeypatch.setattr(
        arm,
        "_rebuild_case_transaction",
        lambda *args, **kwargs: calls.__setitem__("rebuild", calls["rebuild"] + 1),
    )
    monkeypatch.setattr(
        arm,
        "execute_paid_smoke",
        lambda _tx: calls.__setitem__("paid", calls["paid"] + 1),
    )

    with pytest.raises(BenchmarkSafetyError, match="further paid calls are blocked"):
        arm.execute_next(
            repo_root=tmp_path,
            expected_repo_head=HEAD,
            output_root=root,
            expected_plan_sha=PLAN_SHA,
            confirmation=arm.EXECUTE_CONFIRMATION,
        )

    assert calls == {"paid": 0, "rebuild": 0}
    assert complete_path.read_bytes() == paid_evidence_before


def test_case_status_rejects_multiple_terminal_markers(tmp_path):
    case = _case()
    root = tmp_path / "arm"
    case_dir = root / case["case_dir"]
    case_dir.mkdir(parents=True)
    (case_dir / "complete.json").write_text("{}", encoding="utf-8")
    (case_dir / "response_failed.json").write_text("{}", encoding="utf-8")

    with pytest.raises(BenchmarkSafetyError, match="ambiguous terminal markers"):
        arm._case_status(root, case)


def test_execute_next_delegates_exactly_one_paid_transaction(monkeypatch, tmp_path):
    case = _case()
    plan = _plan([case])
    root = tmp_path / "arm"
    root.mkdir()
    (root / arm.JOURNAL_FILENAME).write_bytes(b"")
    tx = {
        "request_sha256": case["flash_request_sha256"],
        "identity_sha256": case["transaction_identity_sha256"],
    }
    result = {
        "status": "complete",
        "provider_response": {
            "response_model": "deepseek-flash",
            "system_fingerprint": "fp-test",
            "finish_reason": "stop",
        },
        "result": {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_cost_usd": 0.001,
            "remaining_cjk_character_count": 0,
        },
        "artifacts": {"translation_sha256": "7" * 64},
    }
    calls = []

    monkeypatch.setattr(
        arm, "_load_arm", lambda *args, **kwargs: (plan, root, PLAN_SHA)
    )
    monkeypatch.setattr(
        arm, "_rebuild_case_transaction", lambda *args, **kwargs: tx
    )
    monkeypatch.setattr(
        arm,
        "execute_paid_smoke",
        lambda received: calls.append(received) or result,
    )

    selected, actual = arm.execute_next(
        repo_root=tmp_path,
        expected_repo_head=HEAD,
        output_root=root,
        expected_plan_sha=PLAN_SHA,
        confirmation=arm.EXECUTE_CONFIRMATION,
    )

    assert selected["case_id"] == case["case_id"]
    assert actual is result
    assert calls == [tx]
    rows = [json.loads(line) for line in (root / arm.JOURNAL_FILENAME).read_text().splitlines()]
    assert rows[-1]["event"] == "case_complete"


def test_execute_next_uncertain_case_blocks_and_is_not_retried(monkeypatch, tmp_path):
    case = _case()
    plan = _plan([case])
    root = tmp_path / "arm"
    root.mkdir()
    (root / arm.JOURNAL_FILENAME).write_bytes(b"")
    case_dir = root / case["case_dir"]
    calls = {"count": 0}

    monkeypatch.setattr(
        arm, "_load_arm", lambda *args, **kwargs: (plan, root, PLAN_SHA)
    )
    monkeypatch.setattr(
        arm, "_rebuild_case_transaction", lambda *args, **kwargs: {"tx": True}
    )

    def fail_once(_tx):
        calls["count"] += 1
        case_dir.mkdir(parents=True)
        (case_dir / "uncertain.json").write_text("{}", encoding="utf-8")
        raise arm.FlashSmokeUncertainError("ambiguous")

    monkeypatch.setattr(arm, "execute_paid_smoke", fail_once)

    with pytest.raises(arm.FlashArmError, match="retry is forbidden"):
        arm.execute_next(
            repo_root=tmp_path,
            expected_repo_head=HEAD,
            output_root=root,
            expected_plan_sha=PLAN_SHA,
            confirmation=arm.EXECUTE_CONFIRMATION,
        )

    assert calls["count"] == 1
    assert arm.arm_status(plan, root)["blocked"] is True


def test_execute_next_refuses_any_paid_call_when_arm_is_blocked(monkeypatch, tmp_path):
    bad = _case(1)
    pending = _case(2, chapter=71)
    plan = _plan([bad, pending])
    root = tmp_path / "arm"
    bad_dir = root / bad["case_dir"]
    bad_dir.mkdir(parents=True)
    (bad_dir / "response_failed.json").write_text("{}", encoding="utf-8")
    calls = {"paid": 0}

    monkeypatch.setattr(
        arm, "_load_arm", lambda *args, **kwargs: (plan, root, PLAN_SHA)
    )
    monkeypatch.setattr(
        arm,
        "execute_paid_smoke",
        lambda _tx: calls.__setitem__("paid", calls["paid"] + 1),
    )

    with pytest.raises(BenchmarkSafetyError, match="further paid calls are blocked"):
        arm.execute_next(
            repo_root=tmp_path,
            expected_repo_head=HEAD,
            output_root=root,
            expected_plan_sha=PLAN_SHA,
            confirmation=arm.EXECUTE_CONFIRMATION,
        )

    assert calls["paid"] == 0


def test_execute_next_rejects_wrong_confirmation_before_loading_arm(monkeypatch, tmp_path):
    loaded = {"count": 0}
    monkeypatch.setattr(
        arm,
        "_load_arm",
        lambda *args, **kwargs: loaded.__setitem__("count", loaded["count"] + 1),
    )

    with pytest.raises(arm.FlashArmError, match="not explicitly authorized"):
        arm.execute_next(
            repo_root=tmp_path,
            expected_repo_head=HEAD,
            output_root=tmp_path / "arm",
            expected_plan_sha=PLAN_SHA,
            confirmation="NO",
        )

    assert loaded["count"] == 0


def test_rebuild_case_rejects_request_identity_drift(monkeypatch, tmp_path):
    case = _case()
    root = tmp_path / "arm"
    root.mkdir()
    (root / "requests").mkdir()
    frozen = root / "requests" / f"{case['case_id']}.json"
    frozen.write_text("{}", encoding="utf-8")
    tx = {
        "row": {
            "source_sha256": case["source_sha256"],
            "relevant_glossary_sha256": case["relevant_glossary_sha256"],
        },
        "pro_request_sha256": case["frozen_pro_request_sha256"],
        "request_sha256": "f" * 64,
        "identity_sha256": case["transaction_identity_sha256"],
        "request": {},
    }
    monkeypatch.setattr(arm, "build_smoke_plan", lambda **kwargs: tx)

    with pytest.raises(BenchmarkSafetyError, match="flash_request_sha256 drifted"):
        arm._rebuild_case_transaction(tmp_path, root, case, HEAD)


def test_confirmation_literal_is_explicit_and_stable():
    assert arm.EXECUTE_CONFIRMATION == "AUTHORIZE_ONE_DEEPSEEK_V41_FLASH_ARM_REQUEST"
