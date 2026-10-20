import copy
import json
from pathlib import Path

import pytest

import benchmark_longitudinal_g7k_fresh_s0_recovery as g7k


def _with_record_hash(value):
    result = copy.deepcopy(value)
    result["record_sha256"] = g7k._sha256_json(result)
    return result


def _with_ledger_hash(value):
    result = copy.deepcopy(value)
    result["ledger_sha256"] = g7k._sha256_json(result)
    return result


def _historical_fixture(tmp_path):
    root = tmp_path / "historical"
    spec = g7k.g7c.stage1_run_specs()[0]
    run_root = root / Path(spec["relative_root"])
    journal_dir = run_root / "data" / "provider_request_journal"
    ledger_dir = run_root / "data" / "g7f_invocation_ledgers"
    chapters = run_root / "output" / "chapters"
    pending = run_root / "output" / "pending"
    for directory in (journal_dir, ledger_dir, chapters, pending):
        directory.mkdir(parents=True, exist_ok=True)

    (run_root / "data" / "progress.json").write_text(
        json.dumps({"last_completed": 0, "next_chapter": 1}),
        encoding="utf-8",
    )

    stats = {
        "prompt_tokens": 6058,
        "completion_tokens": 11994,
        "cache_hit_tokens": 3456,
        "cache_miss_tokens": 2602,
        "cost_usd": 0.007597068,
        "rate_correct_cost_usd": 0.007597068,
        "pricing_version": "fixture",
    }
    response = {
        "content": "",
        "finish_reason": "length",
        "provider_response_id": "fixture-response",
        "provider_response_model": g7k.HISTORICAL_MODEL_ID,
        "stats": stats,
    }

    # Unit fixtures use the frozen response SHA value as the durable field while
    # monkeypatching the generic hash helper for this one fixture response.
    journal = {
        "schema_version": 1,
        "revision": "paid-request-journal-v1",
        "operation_kind": "translation",
        "novel_id": "ace_evolution",
        "chapter": 1,
        "provider": "command_code",
        "model": g7k.HISTORICAL_MODEL_ID,
        "profile_id": "fixture",
        "source_sha256": "1" * 64,
        "translation_sha256": "",
        "request_sha256": g7k.HISTORICAL_REQUEST_SHA256,
        "operation_id": "2" * 64,
        "status": "response_recorded",
        "attempt_context": {},
        "response": response,
        "response_sha256": g7k.HISTORICAL_RESPONSE_SHA256,
        "error": {},
        "created_utc": "2026-09-22T00:00:00+00:00",
        "updated_utc": "2026-09-22T00:00:01+00:00",
    }
    journal = _with_record_hash(journal)
    journal_path = journal_dir / "0001.g7c_translation.json"
    journal_path.write_text(json.dumps(journal), encoding="utf-8")

    ledger = {
        "schema_version": g7k.g7f.LEDGER_SCHEMA_VERSION,
        "revision": g7k.g7f.LEDGER_REVISION,
        "plan_sha256": "3" * 64,
        "approval_token": g7k.HISTORICAL_CONSUMED_APPROVAL_TOKEN,
        "run_index": 1,
        "relative_root": spec["relative_root"],
        "model_id": g7k.HISTORICAL_MODEL_ID,
        "novel_id": "ace_evolution",
        "target_chapter": 1,
        "maximum_new_provider_sends": 7,
        "maximum_credit_equivalent_usd": "0.5",
        "new_provider_sends_started": 1,
        "accepted_credit_equivalent_usd": g7k.HISTORICAL_ACCEPTED_CE_USD,
        "records": [
            {
                "send_index": 1,
                "logical_operation": "translation",
                "chapter": 1,
                "request_sha256": g7k.HISTORICAL_REQUEST_SHA256,
                "request_started_utc": "2026-09-22T00:00:00+00:00",
                "status": "accepted",
                "conservative_cost_bound_usd": "0.1",
                "accepted_cost_usd": g7k.HISTORICAL_ACCEPTED_CE_USD,
                "stats": stats,
                "error": {},
            }
        ],
        "created_utc": "2026-09-22T00:00:00+00:00",
        "updated_utc": "2026-09-22T00:00:01+00:00",
    }
    ledger = _with_ledger_hash(ledger)
    ledger_path = ledger_dir / "0001.json"
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    return root, run_root, journal_path, ledger_path, response


# The frozen Run-1 log artifact slot from the coordinator's forensic proof.
_REAL_LOG_RELATIVE = "logs/run_20260922_052555.jsonl"

# Fixture bytes for the four proven post-S0 incident artifacts.  The real 125-file
# tree cannot be materialized byte-exactly here, so the frozen artifact identity
# table is re-derived from these fixture bytes (the ONLY permitted identity seam).
# The paths themselves must be exactly the frozen forensic paths, or the lookup
# raises, so the fixture can never silently drift away from the real artifact set.
_INCIDENT_ARTIFACT_BYTES = {
    ".production.lock": b"",
    "data/g7f_invocation_ledgers/0001.json": b'{"fixture": "ledger"}\n',
    "data/provider_request_journal/0001.g7c_translation.json": b'{"fixture": "journal"}\n',
    _REAL_LOG_RELATIVE: b'{"fixture": "log"}\n',
}

# A clean post-cleanup staged/fresh stand-in tree is the G7C roots manifest plus
# the ten pristine Run-1 base files; that count is what the canned frozen-S0
# fingerprint is allowed to stand in for.
_CLEAN_TREE_FILE_COUNT = len(g7k.PRISTINE_RUN1_BASE_RELATIVES) + 1


def _real_incident_fixture(tmp_path, name="historical"):
    """Build a miniature historical root carrying the four real incident artifacts."""
    root = tmp_path / name
    spec = g7k.g7c.stage1_run_specs()[0]
    run_root = root / Path(spec["relative_root"])
    root.mkdir(parents=True, exist_ok=True)

    (root / g7k.g7c.ROOTS_MANIFEST_FILENAME).write_text(
        json.dumps({"fixture": "stage1_roots_manifest"}),
        encoding="utf-8",
    )

    for relative in g7k.PRISTINE_RUN1_BASE_RELATIVES:
        path = run_root / Path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        if relative == "data/progress.json":
            path.write_text(
                json.dumps({"last_completed": 0, "next_chapter": 1}),
                encoding="utf-8",
            )
        else:
            path.write_text(f"fixture base bytes for {relative}\n", encoding="utf-8")

    artifact_paths = {}
    for artifact in g7k.HISTORICAL_INCIDENT_ARTIFACTS:
        relative = artifact["relative_posix"]
        path = run_root / Path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_INCIDENT_ARTIFACT_BYTES[relative])
        artifact_paths[relative] = path

    return {"root": root, "run_root": run_root, "artifact_paths": artifact_paths}


def _fixture_incident(
    fixture,
    *,
    file_count=g7k.HISTORICAL_STAGE1_FILE_COUNT,
    fingerprint=g7k.HISTORICAL_STAGE1_TREE_SHA256,
):
    """Return the full verify_historical_incident-shaped evidence for a fixture."""
    journal_path = fixture["artifact_paths"][g7k.HISTORICAL_JOURNAL_RELATIVE]
    ledger_path = fixture["artifact_paths"][g7k.HISTORICAL_LEDGER_RELATIVE]
    return {
        "stage1_tree": {
            "root": str(fixture["root"].resolve()),
            "file_count": file_count,
            "fingerprint": fingerprint,
        },
        "run_root": str(fixture["run_root"].resolve()),
        "progress": {"last_completed": 0, "next_chapter": 1},
        "journal_path": str(journal_path),
        "journal_file_sha256": g7k._sha256_file(journal_path),
        "request_sha256": g7k.HISTORICAL_REQUEST_SHA256,
        "response_sha256": g7k.HISTORICAL_RESPONSE_SHA256,
        "ledger_path": str(ledger_path),
        "ledger_file_sha256": g7k._sha256_file(ledger_path),
        "new_provider_sends_started": 1,
        "accepted_credit_equivalent_usd": g7k.HISTORICAL_ACCEPTED_CE_USD,
        "consumed_approval_token": g7k.HISTORICAL_CONSUMED_APPROVAL_TOKEN,
        "pending_artifact_present": False,
        "final_artifact_present": False,
    }


def _fixture_artifact_table(fixture):
    """Build the fixture-actual artifact identity table in the frozen path order."""
    table = []
    for artifact in g7k.HISTORICAL_INCIDENT_ARTIFACTS:
        relative = artifact["relative_posix"]
        path = fixture["artifact_paths"][relative]
        table.append(
            {
                "relative_posix": relative,
                "size_bytes": path.stat().st_size,
                "sha256": g7k._sha256_file(path),
            }
        )
    return tuple(table)


def _install_reconstruction_seams(
    monkeypatch,
    fixture,
    *,
    artifact_table=None,
    incident=None,
):
    """Install the three permitted seams and nothing else.

    Only the incident evidence, the fixture-actual artifact identity table, and
    the post-cleanup frozen-S0 tree identity are patched.  The historical tree
    gate, the path-set gates, the per-artifact identity checks, the unlink and
    directory rules, and the final gate all run as real production code.
    """
    if artifact_table is None:
        artifact_table = _fixture_artifact_table(fixture)
    if incident is None:
        incident = _fixture_incident(fixture)

    monkeypatch.setattr(g7k, "HISTORICAL_INCIDENT_ARTIFACTS", artifact_table)
    monkeypatch.setattr(
        g7k,
        "verify_historical_incident",
        lambda _root: copy.deepcopy(incident),
    )

    real_tree = g7k.tree_fingerprint

    def fake_tree(root):
        identity = real_tree(root)
        if identity["file_count"] == _CLEAN_TREE_FILE_COUNT:
            return {
                "root": identity["root"],
                "file_count": g7k.EXPECTED_FRESH_S0_FILE_COUNT,
                "fingerprint": g7k.EXPECTED_FRESH_S0_TREE_SHA256,
            }
        return identity

    monkeypatch.setattr(g7k, "tree_fingerprint", fake_tree)
    return artifact_table, incident


def _relative_files(root):
    resolved = Path(root).resolve()
    return {
        path.resolve().relative_to(resolved).as_posix()
        for path in resolved.rglob("*")
        if path.is_file()
    }


def _staging_leftovers(parent):
    return [child.name for child in Path(parent).iterdir() if ".reconstructing-" in child.name]


def test_policy_identity_is_zero_provider_and_preserves_frozen_contracts():
    identity = g7k.g7k_policy_identity()

    assert identity["revision"] == g7k.G7K_REVISION
    assert identity["provider_boundary"] == {
        "provider_calls_authorized": False,
        "provider_calls": 0,
        "credential_access": False,
        "chapter_execution": False,
        "retry_authorized": False,
    }
    assert (
        identity["fresh_generation_requirement"]["g7c_manifest_must_reproduce"]
        == g7k.g7f.EXPECTED_STAGE1_ROOTS_MANIFEST_SHA256
    )
    assert (
        identity["fresh_generation_requirement"]["expected_tree_sha256"]
        == g7k.EXPECTED_FRESH_S0_TREE_SHA256
    )
    assert identity["authorization_evidence"][
        "executor_token_may_repeat_on_identical_pristine_state"
    ] is True
    assert identity["fresh_generation_requirement"][
        "current_mutable_production_guard_not_required_to_match_historical"
    ] is True
    assert identity["fresh_generation_requirement"][
        "current_immutable_source_bindings_must_match_frozen_manifest"
    ] is True


def test_tree_fingerprint_is_path_independent_for_same_relative_bytes(tmp_path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    for root in (left, right):
        (root / "nested").mkdir(parents=True)
        (root / "a.txt").write_text("alpha", encoding="utf-8")
        (root / "nested" / "b.txt").write_text("beta", encoding="utf-8")

    a = g7k.tree_fingerprint(left)
    b = g7k.tree_fingerprint(right)

    assert a["file_count"] == b["file_count"] == 2
    assert a["fingerprint"] == b["fingerprint"]


def test_verify_historical_incident_checks_response_body_and_ledger(monkeypatch, tmp_path):
    root, _run_root, _journal_path, _ledger_path, response = _historical_fixture(
        tmp_path
    )
    real_hash = g7k._sha256_json

    def fixture_hash(value):
        if isinstance(value, dict) and value == response:
            return g7k.HISTORICAL_RESPONSE_SHA256
        return real_hash(value)

    monkeypatch.setattr(g7k, "_sha256_json", fixture_hash)
    result = g7k.verify_historical_incident(root)

    assert result["request_sha256"] == g7k.HISTORICAL_REQUEST_SHA256
    assert result["response_sha256"] == g7k.HISTORICAL_RESPONSE_SHA256
    assert result["new_provider_sends_started"] == 1
    assert result["accepted_credit_equivalent_usd"] == "0.007597068"
    assert result["pending_artifact_present"] is False
    assert result["final_artifact_present"] is False


def test_historical_incident_rejects_progress_drift(monkeypatch, tmp_path):
    root, run_root, _journal_path, _ledger_path, response = _historical_fixture(
        tmp_path
    )
    real_hash = g7k._sha256_json

    def fixture_hash(value):
        if isinstance(value, dict) and value == response:
            return g7k.HISTORICAL_RESPONSE_SHA256
        return real_hash(value)

    monkeypatch.setattr(g7k, "_sha256_json", fixture_hash)
    (run_root / "data" / "progress.json").write_text(
        json.dumps({"last_completed": 1, "next_chapter": 2}),
        encoding="utf-8",
    )

    with pytest.raises(g7k.G7KRecoveryError, match="progress"):
        g7k.verify_historical_incident(root)


def test_recovery_binding_distinguishes_generation_even_if_executor_token_repeats():
    historical = {
        "stage1_tree": {"fingerprint": "a" * 64},
    }
    fresh = {
        "tree": {"fingerprint": g7k.EXPECTED_FRESH_S0_TREE_SHA256},
        "g7c_manifest_sha256": g7k.g7f.EXPECTED_STAGE1_ROOTS_MANIFEST_SHA256,
    }
    plan = {
        "plan_sha256": "b" * 64,
        "approval_token": g7k.HISTORICAL_CONSUMED_APPROVAL_TOKEN,
    }

    one = g7k._recovery_binding_payload(
        recovery_generation_id="g7k-generation-one",
        historical=historical,
        fresh=fresh,
        plan=plan,
    )
    two = g7k._recovery_binding_payload(
        recovery_generation_id="g7k-generation-two",
        historical=historical,
        fresh=fresh,
        plan=plan,
    )

    assert one["fresh_g7e_approval_token"] == two["fresh_g7e_approval_token"]
    assert g7k._sha256_json(one) != g7k._sha256_json(two)


def test_prepare_composes_zero_provider_fresh_generation(monkeypatch, tmp_path):
    production = tmp_path / "production"
    historical = tmp_path / "historical"
    fresh = tmp_path / "fresh"
    evidence = tmp_path / "evidence"
    production.mkdir()
    historical.mkdir()

    historical_identity = {
        "stage1_tree": {
            "root": str(historical.resolve()),
            "file_count": 3,
            "fingerprint": "a" * 64,
        },
        "run_root": str((historical / "run").resolve()),
        "progress": {"last_completed": 0, "next_chapter": 1},
        "journal_path": str(historical / "journal.json"),
        "journal_file_sha256": "b" * 64,
        "request_sha256": g7k.HISTORICAL_REQUEST_SHA256,
        "response_sha256": g7k.HISTORICAL_RESPONSE_SHA256,
        "ledger_path": str(historical / "ledger.json"),
        "ledger_file_sha256": "c" * 64,
        "new_provider_sends_started": 1,
        "accepted_credit_equivalent_usd": g7k.HISTORICAL_ACCEPTED_CE_USD,
        "consumed_approval_token": g7k.HISTORICAL_CONSUMED_APPROVAL_TOKEN,
        "pending_artifact_present": False,
        "final_artifact_present": False,
    }
    monkeypatch.setattr(
        g7k,
        "verify_historical_incident",
        lambda _root: copy.deepcopy(historical_identity),
    )

    def fake_reconstruct(*, historical_stage1_root, fresh_stage1_root):
        Path(fresh_stage1_root).mkdir(parents=True)
        return {
            "method": "historical_exact_copy_minus_allowlisted_incident_artifacts",
            "removed_from_copy": [
                ".production.lock",
                "data/g7f_invocation_ledgers/0001.json",
                "data/provider_request_journal/0001.g7c_translation.json",
                "logs/run_20260922_052555.jsonl",
                "data/g7f_invocation_ledgers/",
            ],
            "provider_calls": 0,
            "credential_access": False,
            "historical_stage1_modified": False,
            "tree": {
                "root": str(Path(fresh_stage1_root).resolve()),
                "file_count": 121,
                "fingerprint": g7k.EXPECTED_FRESH_S0_TREE_SHA256,
            },
        }

    monkeypatch.setattr(
        g7k,
        "reconstruct_fresh_s0_from_historical",
        fake_reconstruct,
    )

    fresh_identity = {
        "g7c_manifest_sha256": g7k.g7f.EXPECTED_STAGE1_ROOTS_MANIFEST_SHA256,
        "run_count": 12,
        "frozen_source_bindings_verified": True,
        "historical_manifest_reproduced": True,
        "provider_calls": 0,
        "paid_stage1_authorized": False,
        "tree": {
            "root": str(fresh.resolve()),
            "file_count": 121,
            "fingerprint": g7k.EXPECTED_FRESH_S0_TREE_SHA256,
        },
    }
    monkeypatch.setattr(
        g7k,
        "verify_fresh_s0",
        lambda **_kwargs: copy.deepcopy(fresh_identity),
    )

    plan = {
        "plan_sha256": "d" * 64,
        "approval_token": g7k.HISTORICAL_CONSUMED_APPROVAL_TOKEN,
        "g7e_plan": {
            "maximum_new_provider_sends": 7,
            "maximum_credit_equivalent_usd": "0.5",
        },
    }
    monkeypatch.setattr(
        g7k.g7f,
        "build_stage1_invocation_plan",
        lambda **_kwargs: copy.deepcopy(plan),
    )
    monkeypatch.setattr(
        g7k.g7f,
        "inspect_stage1_invocation_plan",
        lambda **_kwargs: {
            "provider_calls": 0,
            "progress": {"last_completed": 0, "next_chapter": 1},
        },
    )

    original_verify = g7k.verify_recovery

    def fake_final_verify(**kwargs):
        manifest = json.loads(
            (Path(kwargs["evidence_root"]) / g7k.RECOVERY_MANIFEST_FILENAME)
            .read_text(encoding="utf-8")
        )
        return {
            "status": "READY_FOR_FRESH_OPERATOR_AUTHORIZATION",
            "g7k_policy_sha256": g7k.G7K_POLICY_SHA256,
            "recovery_manifest_sha256": manifest["recovery_manifest_sha256"],
            "recovery_generation_id": manifest["recovery_generation_id"],
            "recovery_binding_sha256": manifest["fresh_operator_authorization"][
                "recovery_binding_sha256"
            ],
            "historical_stage1_tree_sha256": "a" * 64,
            "fresh_stage1_tree_sha256": g7k.EXPECTED_FRESH_S0_TREE_SHA256,
            "fresh_stage1_file_count": 121,
            "fresh_g7c_manifest_sha256": g7k.g7f.EXPECTED_STAGE1_ROOTS_MANIFEST_SHA256,
            "plan_sha256": "d" * 64,
            "approval_token": g7k.HISTORICAL_CONSUMED_APPROVAL_TOKEN,
            "executor_approval_token_equals_consumed_historical_token": True,
            "maximum_new_provider_sends": 7,
            "maximum_credit_equivalent_usd": "0.5",
            "provider_calls": 0,
            "credential_access": False,
            "paid_execution_authorized": False,
            "chapter_1_executed": False,
            "historical_stage1_modified": False,
            "new_operator_authorization_required": True,
        }

    monkeypatch.setattr(g7k, "verify_recovery", fake_final_verify)
    result = g7k.prepare_fresh_s0_recovery(
        production_root=production,
        historical_stage1_root=historical,
        fresh_stage1_root=fresh,
        evidence_root=evidence,
        recovery_generation_id="g7k-test-generation",
        maximum_new_provider_sends=7,
        maximum_credit_equivalent_usd="0.5",
    )
    monkeypatch.setattr(g7k, "verify_recovery", original_verify)

    assert result["provider_calls"] == 0
    assert result["credential_access"] is False
    assert result["new_operator_authorization_required"] is True
    manifest = json.loads(
        (evidence / g7k.RECOVERY_MANIFEST_FILENAME).read_text(encoding="utf-8")
    )
    assert manifest["fresh_operator_authorization"]["required"] is True
    assert manifest["fresh_operator_authorization"]["granted"] is False
    assert manifest["historical"]["modified_by_g7k"] is False
    assert manifest["safety"]["provider_calls"] == 0

    reconstruction = manifest["fresh"]["reconstruction"]
    assert reconstruction["removed_from_copy"] == [
        ".production.lock",
        "data/g7f_invocation_ledgers/0001.json",
        "data/provider_request_journal/0001.g7c_translation.json",
        "logs/run_20260922_052555.jsonl",
        "data/g7f_invocation_ledgers/",
    ]
    assert reconstruction["provider_calls"] == 0
    assert reconstruction["credential_access"] is False
    assert reconstruction["historical_stage1_modified"] is False


def test_prepare_refuses_existing_fresh_or_evidence_root(tmp_path):
    production = tmp_path / "production"
    historical = tmp_path / "historical"
    fresh = tmp_path / "fresh"
    evidence = tmp_path / "evidence"
    for path in (production, historical, fresh):
        path.mkdir()

    with pytest.raises(g7k.G7KRecoveryError, match="fresh Stage-1 root already exists"):
        g7k.prepare_fresh_s0_recovery(
            production_root=production,
            historical_stage1_root=historical,
            fresh_stage1_root=fresh,
            evidence_root=evidence,
            recovery_generation_id="g7k-test-generation",
            maximum_new_provider_sends=7,
            maximum_credit_equivalent_usd="0.5",
        )


def test_parser_exposes_no_paid_or_credential_flags():
    parser = g7k.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "prepare",
                "--production-root", "p",
                "--historical-stage1-root", "h",
                "--fresh-stage1-root", "f",
                "--evidence-root", "e",
                "--recovery-generation-id", "g7k-test-generation",
                "--maximum-new-provider-sends", "7",
                "--maximum-credit-equivalent-usd", "0.5",
                "--authorize-paid-run",
            ]
        )
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "verify",
                "--production-root", "p",
                "--historical-stage1-root", "h",
                "--fresh-stage1-root", "f",
                "--evidence-root", "e",
                "--api-key", "secret",
            ]
        )


def test_render_summary_keeps_authorization_closed():
    summary = g7k.render_summary(
        {
            "status": "READY_FOR_FRESH_OPERATOR_AUTHORIZATION",
            "g7k_policy_sha256": "a" * 64,
            "recovery_generation_id": "g7k-test-generation",
            "recovery_binding_sha256": "b" * 64,
            "historical_stage1_tree_sha256": "c" * 64,
            "fresh_stage1_file_count": 121,
            "fresh_stage1_tree_sha256": "d" * 64,
            "fresh_g7c_manifest_sha256": "e" * 64,
            "plan_sha256": "f" * 64,
            "approval_token": "0" * 64,
            "executor_approval_token_equals_consumed_historical_token": True,
            "maximum_new_provider_sends": 7,
            "maximum_credit_equivalent_usd": "0.5",
        }
    )

    assert "G7K_RECOVERY=PASS" in summary
    assert "BENCHMARK_PROVIDER_CALLS=0" in summary
    assert "CREDENTIAL_ACCESS=NO" in summary
    assert "PAID_EXECUTION_AUTHORIZED=NO" in summary
    assert "CHAPTER_1_EXECUTED=NO" in summary
    assert "HISTORICAL_STAGE1_MODIFIED=NO" in summary
    assert "NEW_OPERATOR_AUTHORIZATION_REQUIRED=YES" in summary


def test_incident_artifact_model_matches_frozen_forensic_facts():
    expected = (
        {
            "relative_posix": ".production.lock",
            "size_bytes": 0,
            "sha256": (
                "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
            ),
        },
        {
            "relative_posix": "data/g7f_invocation_ledgers/0001.json",
            "size_bytes": 2230,
            "sha256": (
                "ae6ebb7c0eab47fd8b308cb22a209f861f750c6d43045102c31386237427dbb5"
            ),
        },
        {
            "relative_posix": "data/provider_request_journal/0001.g7c_translation.json",
            "size_bytes": 2601,
            "sha256": (
                "5aa79041d4b00523d064d8d49adf6b4767726a901402cf656c93d9f04aa2f719"
            ),
        },
        {
            "relative_posix": "logs/run_20260922_052555.jsonl",
            "size_bytes": 1099,
            "sha256": (
                "474b0a74fe745f6aec83fd07366325778db16268df9703a099fe1c7cf6d5101d"
            ),
        },
    )
    table = tuple(dict(artifact) for artifact in g7k.HISTORICAL_INCIDENT_ARTIFACTS)

    # Every field of every artifact is a frozen forensic literal.
    assert table == expected
    assert len(table) == 4

    relatives = [artifact["relative_posix"] for artifact in table]
    # The journal/ledger artifact entries ARE the frozen relative paths.
    assert g7k.HISTORICAL_JOURNAL_RELATIVE in relatives
    assert g7k.HISTORICAL_LEDGER_RELATIVE in relatives
    assert _REAL_LOG_RELATIVE in relatives
    assert Path(g7k.HISTORICAL_LEDGER_RELATIVE).parent.as_posix() == (
        g7k.REMOVED_LEDGER_DIRECTORY_RELATIVE
    )

    # Exactly ten base paths union four artifact paths, with no overlap.
    assert len(g7k.PRISTINE_RUN1_BASE_RELATIVES) == 10
    assert len(set(g7k.PRISTINE_RUN1_BASE_RELATIVES)) == 10
    assert len(set(relatives)) == 4
    assert g7k.EXPECTED_RUN1_RELATIVE_FILES == frozenset(
        g7k.PRISTINE_RUN1_BASE_RELATIVES
    ) | frozenset(relatives)
    assert len(g7k.EXPECTED_RUN1_RELATIVE_FILES) == 14

    # The removed ledger directory stays in sync with the executor ledger path.
    assert g7k.REMOVED_LEDGER_DIRECTORY_RELATIVE == g7k.g7f.LEDGER_DIRECTORY


def test_frozen_historical_tree_identity_constants_match_forensics():
    assert g7k.HISTORICAL_STAGE1_FILE_COUNT == 125
    assert g7k.HISTORICAL_STAGE1_TREE_SHA256 == (
        "7105190009524ca4a7742ad5931addabd05f7621f28de3bc0a0b368f5b28ffe7"
    )
    assert g7k.EXPECTED_FRESH_S0_FILE_COUNT == 121
    assert g7k.EXPECTED_FRESH_S0_TREE_SHA256 == (
        "12a81545d0a43fabb42462bc45f5b55963be2b5c97a07b49cc3297ea320da0a8"
    )
    assert g7k.HISTORICAL_STAGE1_TREE_SHA256 != g7k.EXPECTED_FRESH_S0_TREE_SHA256


def test_historical_tree_identity_gate_fails_closed(monkeypatch, tmp_path):
    fixture = _real_incident_fixture(tmp_path)
    fresh = tmp_path / "fresh"

    for file_count, fingerprint in (
        (g7k.HISTORICAL_STAGE1_FILE_COUNT, "0" * 64),
        (g7k.HISTORICAL_STAGE1_FILE_COUNT - 1, g7k.HISTORICAL_STAGE1_TREE_SHA256),
    ):
        incident = _fixture_incident(
            fixture, file_count=file_count, fingerprint=fingerprint
        )
        monkeypatch.setattr(
            g7k,
            "verify_historical_incident",
            lambda _root, _incident=incident: copy.deepcopy(_incident),
        )
        with pytest.raises(
            g7k.G7KRecoveryError, match="does not match the frozen incident tree"
        ):
            g7k.reconstruct_fresh_s0_from_historical(
                historical_stage1_root=fixture["root"],
                fresh_stage1_root=fresh,
            )

    assert not fresh.exists()
    assert _staging_leftovers(tmp_path) == []


def test_reconstruction_removes_exactly_the_four_real_incident_artifacts(monkeypatch, tmp_path):
    fixture = _real_incident_fixture(tmp_path)
    fresh = tmp_path / "fresh"
    _install_reconstruction_seams(monkeypatch, fixture)

    result = g7k.reconstruct_fresh_s0_from_historical(
        historical_stage1_root=fixture["root"],
        fresh_stage1_root=fresh,
    )

    assert result["method"] == (
        "historical_exact_copy_minus_allowlisted_incident_artifacts"
    )
    assert result["removed_from_copy"] == [
        ".production.lock",
        "data/g7f_invocation_ledgers/0001.json",
        "data/provider_request_journal/0001.g7c_translation.json",
        "logs/run_20260922_052555.jsonl",
        "data/g7f_invocation_ledgers/",
    ]
    assert all(isinstance(entry, str) for entry in result["removed_from_copy"])
    assert result["provider_calls"] == 0
    assert result["credential_access"] is False
    assert result["historical_stage1_modified"] is False
    # The real final gate accepted the post-cleanup tree as the frozen 121-file S0.
    assert result["tree"]["file_count"] == g7k.EXPECTED_FRESH_S0_FILE_COUNT
    assert result["tree"]["fingerprint"] == g7k.EXPECTED_FRESH_S0_TREE_SHA256

    fresh_run_root = g7k._run_root(fresh)
    assert _relative_files(fresh_run_root) == set(g7k.PRISTINE_RUN1_BASE_RELATIVES)
    assert {path.name for path in fresh.iterdir() if path.is_file()} == {
        g7k.g7c.ROOTS_MANIFEST_FILENAME
    }
    for artifact in g7k.HISTORICAL_INCIDENT_ARTIFACTS:
        assert not (fresh_run_root / Path(artifact["relative_posix"])).exists()
    assert not (fresh_run_root / Path(g7k.REMOVED_LEDGER_DIRECTORY_RELATIVE)).exists()
    assert (fresh_run_root / "logs").is_dir()
    assert list((fresh_run_root / "logs").iterdir()) == []

    for relative in g7k.PRISTINE_RUN1_BASE_RELATIVES:
        assert (fresh_run_root / Path(relative)).read_bytes() == (
            fixture["run_root"] / Path(relative)
        ).read_bytes()

    assert _staging_leftovers(tmp_path) == []


@pytest.mark.parametrize(
    "relative",
    [artifact["relative_posix"] for artifact in g7k.HISTORICAL_INCIDENT_ARTIFACTS],
)
def test_reconstruction_fails_closed_when_an_expected_artifact_is_missing(
    monkeypatch, tmp_path, relative
):
    fixture = _real_incident_fixture(tmp_path)
    fresh = tmp_path / "fresh"
    _install_reconstruction_seams(monkeypatch, fixture)
    fixture["artifact_paths"][relative].unlink()

    with pytest.raises(g7k.G7KRecoveryError) as excinfo:
        g7k.reconstruct_fresh_s0_from_historical(
            historical_stage1_root=fixture["root"],
            fresh_stage1_root=fresh,
        )

    assert relative in str(excinfo.value)
    assert not fresh.exists()
    assert _staging_leftovers(tmp_path) == []
    for other, path in fixture["artifact_paths"].items():
        if other != relative:
            assert path.is_file()


@pytest.mark.parametrize(
    "relative, appended",
    [
        (".production.lock", b"\x00"),
        (g7k.HISTORICAL_LEDGER_RELATIVE, b" "),
        (g7k.HISTORICAL_JOURNAL_RELATIVE, b" "),
        (_REAL_LOG_RELATIVE, b" "),
    ],
)
def test_reconstruction_fails_closed_when_an_expected_artifact_is_modified(
    monkeypatch, tmp_path, relative, appended
):
    fixture = _real_incident_fixture(tmp_path)
    fresh = tmp_path / "fresh"
    table = _fixture_artifact_table(fixture)
    incident = _fixture_incident(fixture)
    _install_reconstruction_seams(
        monkeypatch, fixture, artifact_table=table, incident=incident
    )

    path = fixture["artifact_paths"][relative]
    path.write_bytes(path.read_bytes() + appended)

    with pytest.raises(g7k.G7KRecoveryError) as excinfo:
        g7k.reconstruct_fresh_s0_from_historical(
            historical_stage1_root=fixture["root"],
            fresh_stage1_root=fresh,
        )

    assert relative in str(excinfo.value)
    assert not fresh.exists()
    assert _staging_leftovers(tmp_path) == []
    assert path.read_bytes().endswith(appended)


def test_reconstruction_fails_closed_on_unexpected_extra_artifact(monkeypatch, tmp_path):
    # An unexpected extra file inside the Run-1 root breaks the frozen path set.
    run1 = _real_incident_fixture(tmp_path, name="historical-run1")
    _install_reconstruction_seams(monkeypatch, run1)
    (run1["run_root"] / "data" / "unexpected.json").write_text("{}", encoding="utf-8")

    with pytest.raises(
        g7k.G7KRecoveryError, match="not the frozen incident set"
    ) as excinfo:
        g7k.reconstruct_fresh_s0_from_historical(
            historical_stage1_root=run1["root"],
            fresh_stage1_root=tmp_path / "fresh-run1",
        )
    assert "data/unexpected.json" in str(excinfo.value)
    assert not (tmp_path / "fresh-run1").exists()

    # An unexpected extra file at the Stage-1 root level breaks the manifest gate.
    root_level = _real_incident_fixture(tmp_path, name="historical-rootlevel")
    _install_reconstruction_seams(monkeypatch, root_level)
    (root_level["root"] / "unexpected.txt").write_text("x", encoding="utf-8")

    with pytest.raises(g7k.G7KRecoveryError, match="root-level file set") as excinfo:
        g7k.reconstruct_fresh_s0_from_historical(
            historical_stage1_root=root_level["root"],
            fresh_stage1_root=tmp_path / "fresh-rootlevel",
        )
    assert "unexpected.txt" in str(excinfo.value)
    assert not (tmp_path / "fresh-rootlevel").exists()

    # An unexpected extra file inside a run 2..12 root is not part of any run-1
    # path set, so only the frozen Stage-1 S0 count gate can catch it.
    spec = g7k.g7c.stage1_run_specs()[1]
    other_run = _real_incident_fixture(tmp_path, name="historical-run2")
    _install_reconstruction_seams(monkeypatch, other_run)
    second_run_root = other_run["root"] / Path(spec["relative_root"])
    second_run_root.mkdir(parents=True)
    (second_run_root / "unexpected.json").write_text("{}", encoding="utf-8")

    with pytest.raises(
        g7k.G7KRecoveryError, match="frozen S0 file count"
    ) as excinfo:
        g7k.reconstruct_fresh_s0_from_historical(
            historical_stage1_root=other_run["root"],
            fresh_stage1_root=tmp_path / "fresh-run2",
        )
    assert not (tmp_path / "fresh-run2").exists()
    assert _staging_leftovers(tmp_path) == []

    # Baseline: the identical fixture shape without the extra run-2 file is
    # accepted, so the extra artifact is what made the reconstruction fail closed.
    baseline = _real_incident_fixture(tmp_path, name="historical-run2-baseline")
    _install_reconstruction_seams(monkeypatch, baseline)
    baseline_result = g7k.reconstruct_fresh_s0_from_historical(
        historical_stage1_root=baseline["root"],
        fresh_stage1_root=tmp_path / "fresh-run2-baseline",
    )
    assert baseline_result["tree"]["fingerprint"] == g7k.EXPECTED_FRESH_S0_TREE_SHA256


def test_reconstruction_fails_closed_on_wrong_artifact_hash_or_size(monkeypatch, tmp_path):
    # A non-empty .production.lock violates the real frozen empty-file literal.
    real_table = _real_incident_fixture(tmp_path, name="historical-real-table")
    fresh_a = tmp_path / "fresh-a"
    _install_reconstruction_seams(
        monkeypatch,
        real_table,
        artifact_table=g7k.HISTORICAL_INCIDENT_ARTIFACTS,
    )
    lock = real_table["artifact_paths"][".production.lock"]
    assert lock.stat().st_size == 0
    lock.write_bytes(b"\x00")

    with pytest.raises(
        g7k.G7KRecoveryError, match="artifact size changed"
    ) as excinfo:
        g7k.reconstruct_fresh_s0_from_historical(
            historical_stage1_root=real_table["root"],
            fresh_stage1_root=fresh_a,
        )
    assert ".production.lock" in str(excinfo.value)
    assert not fresh_a.exists()

    ledger_index = next(
        index
        for index, artifact in enumerate(g7k.HISTORICAL_INCIDENT_ARTIFACTS)
        if artifact["relative_posix"] == g7k.HISTORICAL_LEDGER_RELATIVE
    )

    # A wrong frozen size for one artifact.
    sized = _real_incident_fixture(tmp_path, name="historical-wrong-size")
    fresh_b = tmp_path / "fresh-b"
    sized_table = [dict(artifact) for artifact in _fixture_artifact_table(sized)]
    sized_table[ledger_index]["size_bytes"] += 1
    _install_reconstruction_seams(
        monkeypatch, sized, artifact_table=tuple(sized_table)
    )

    with pytest.raises(
        g7k.G7KRecoveryError, match="artifact size changed"
    ) as excinfo:
        g7k.reconstruct_fresh_s0_from_historical(
            historical_stage1_root=sized["root"],
            fresh_stage1_root=fresh_b,
        )
    assert g7k.HISTORICAL_LEDGER_RELATIVE in str(excinfo.value)
    assert not fresh_b.exists()

    # A wrong frozen SHA-256 with the correct frozen size for one artifact.
    hashed = _real_incident_fixture(tmp_path, name="historical-wrong-sha")
    fresh_c = tmp_path / "fresh-c"
    hashed_table = [dict(artifact) for artifact in _fixture_artifact_table(hashed)]
    hashed_table[ledger_index]["sha256"] = "0" * 64
    _install_reconstruction_seams(
        monkeypatch, hashed, artifact_table=tuple(hashed_table)
    )

    with pytest.raises(
        g7k.G7KRecoveryError, match="artifact SHA-256 changed"
    ) as excinfo:
        g7k.reconstruct_fresh_s0_from_historical(
            historical_stage1_root=hashed["root"],
            fresh_stage1_root=fresh_c,
        )
    assert g7k.HISTORICAL_LEDGER_RELATIVE in str(excinfo.value)
    assert not fresh_c.exists()
    assert _staging_leftovers(tmp_path) == []


def test_reconstruction_reverifies_artifact_identity_in_the_copy(monkeypatch, tmp_path):
    fixture = _real_incident_fixture(tmp_path)
    fresh = tmp_path / "fresh"
    table = _fixture_artifact_table(fixture)
    incident = _fixture_incident(fixture)
    _install_reconstruction_seams(
        monkeypatch, fixture, artifact_table=table, incident=incident
    )

    journal_name = Path(g7k.HISTORICAL_JOURNAL_RELATIVE).name
    real_sha256_file = g7k._sha256_file

    def staged_journal_digest(path):
        target = Path(path)
        if target.name == journal_name and any(
            ".reconstructing-" in part for part in target.parts
        ):
            return "0" * 64
        return real_sha256_file(target)

    monkeypatch.setattr(g7k, "_sha256_file", staged_journal_digest)

    with pytest.raises(
        g7k.G7KRecoveryError, match="copied historical incident artifact bytes changed"
    ) as excinfo:
        g7k.reconstruct_fresh_s0_from_historical(
            historical_stage1_root=fixture["root"],
            fresh_stage1_root=fresh,
        )

    assert g7k.HISTORICAL_JOURNAL_RELATIVE in str(excinfo.value)
    assert not fresh.exists()
    assert _staging_leftovers(tmp_path) == []
    for relative, path in fixture["artifact_paths"].items():
        assert path.read_bytes() == _INCIDENT_ARTIFACT_BYTES[relative]


def test_reconstruction_never_modifies_the_historical_root(monkeypatch, tmp_path):
    fixture = _real_incident_fixture(tmp_path)
    fresh = tmp_path / "fresh"
    real_tree = g7k.tree_fingerprint

    before_tree = real_tree(fixture["root"])
    before_files = sorted(_relative_files(fixture["root"]))
    before_bytes = {
        relative: path.read_bytes()
        for relative, path in fixture["artifact_paths"].items()
    }

    _install_reconstruction_seams(monkeypatch, fixture)
    result = g7k.reconstruct_fresh_s0_from_historical(
        historical_stage1_root=fixture["root"],
        fresh_stage1_root=fresh,
    )

    assert result["historical_stage1_modified"] is False

    after_tree = real_tree(fixture["root"])
    assert after_tree == before_tree
    assert after_tree["file_count"] != g7k.EXPECTED_FRESH_S0_FILE_COUNT
    assert sorted(_relative_files(fixture["root"])) == before_files
    assert _relative_files(fixture["run_root"]) == set(g7k.EXPECTED_RUN1_RELATIVE_FILES)
    assert {path.name for path in fixture["root"].iterdir() if path.is_file()} == {
        g7k.g7c.ROOTS_MANIFEST_FILENAME
    }

    for relative, data in before_bytes.items():
        assert fixture["artifact_paths"][relative].read_bytes() == data

    historical_ledger_dir = fixture["run_root"] / Path(
        g7k.REMOVED_LEDGER_DIRECTORY_RELATIVE
    )
    assert historical_ledger_dir.is_dir()
    assert (historical_ledger_dir / "0001.json").is_file()


def test_reconstruction_only_removes_allowlisted_incident_artifacts(monkeypatch, tmp_path):
    """Only the four artifacts plus the emptied ledger directory leave the copy."""
    fixture = _real_incident_fixture(tmp_path)
    fresh = tmp_path / "fresh"
    _install_reconstruction_seams(monkeypatch, fixture)

    result = g7k.reconstruct_fresh_s0_from_historical(
        historical_stage1_root=fixture["root"],
        fresh_stage1_root=fresh,
    )

    assert result["removed_from_copy"] == [
        ".production.lock",
        "data/g7f_invocation_ledgers/0001.json",
        "data/provider_request_journal/0001.g7c_translation.json",
        "logs/run_20260922_052555.jsonl",
        "data/g7f_invocation_ledgers/",
    ]
    assert result["historical_stage1_modified"] is False

    manifest = fixture["root"] / g7k.g7c.ROOTS_MANIFEST_FILENAME
    assert manifest.is_file()
    assert (fresh / g7k.g7c.ROOTS_MANIFEST_FILENAME).is_file()

    fresh_run_root = g7k._run_root(fresh)
    for relative, path in fixture["artifact_paths"].items():
        assert path.is_file()
        assert not (fresh_run_root / Path(relative)).exists()
    assert fixture["run_root"] / Path(g7k.REMOVED_LEDGER_DIRECTORY_RELATIVE) / (
        "0001.json"
    ) in list(
        (fixture["run_root"] / Path(g7k.REMOVED_LEDGER_DIRECTORY_RELATIVE)).iterdir()
    )
    assert not (fresh_run_root / Path(g7k.REMOVED_LEDGER_DIRECTORY_RELATIVE)).exists()
    assert (fresh_run_root / "logs").is_dir()
    assert list((fresh_run_root / "logs").iterdir()) == []


def test_historical_journal_operation_kind_is_logical_translation_not_slot_name(
    monkeypatch, tmp_path
):
    # The deterministic journal filename/slot is g7c_translation, but the durable
    # record stores the logical lifecycle operation itself.
    assert g7k.HISTORICAL_JOURNAL_RELATIVE.endswith(
        "0001.g7c_translation.json"
    )
    assert g7k.LIFECYCLE_OPERATION_TRANSLATION == "translation"

    slot_entries = [
        artifact
        for artifact in g7k.HISTORICAL_INCIDENT_ARTIFACTS
        if artifact["relative_posix"].endswith(".g7c_translation.json")
    ]
    assert len(slot_entries) == 1
    assert slot_entries[0]["relative_posix"] == g7k.HISTORICAL_JOURNAL_RELATIVE
    assert slot_entries[0]["relative_posix"] != g7k.LIFECYCLE_OPERATION_TRANSLATION
    assert g7k.LIFECYCLE_OPERATION_TRANSLATION in slot_entries[0]["relative_posix"]

    # The real incident verifier binds the logical operation kind, not the slot.
    root, _run_root, journal_path, _ledger_path, response = _historical_fixture(
        tmp_path
    )
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    journal["operation_kind"] = "g7c_translation"
    journal = _with_record_hash(
        {key: value for key, value in journal.items() if key != "record_sha256"}
    )
    journal_path.write_text(json.dumps(journal), encoding="utf-8")

    real_hash = g7k._sha256_json

    def fixture_hash(value):
        if isinstance(value, dict) and value == response:
            return g7k.HISTORICAL_RESPONSE_SHA256
        return real_hash(value)

    monkeypatch.setattr(g7k, "_sha256_json", fixture_hash)

    with pytest.raises(g7k.G7KRecoveryError, match="operation kind"):
        g7k.verify_historical_incident(root)
