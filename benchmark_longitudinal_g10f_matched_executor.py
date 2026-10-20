"""G10F paid matched-literary executor (arm G10A), one frozen case per invocation.

G10F executes exactly one of the 102 frozen matched-literary requests from
``benchmark_longitudinal_g10_matched``. Importing this module performs no
credential read, no client construction, no network activity, and no filesystem
write.

Fixed execution order for one authorized invocation:

1. bind the live frozen G10E artifacts read-only (freeze ID, freeze-file SHA-256,
   budget-ledger SHA-256, matched-plan SHA-256, G10B S0 manifest SHA-256, pricing,
   ratecard) and pin them into the plan;
2. verify the plan envelope, the exact plan SHA-256, the exact approval token, the
   re-materialized frozen case's request body SHA-256, the unchanged target-state
   fingerprint, and -- only when this token has already been consumed by a started
   transport -- refuse a second send under that token;
3. apply the explicit operator authorization gate. With no operator authorization
   nothing else happens: no client, no ledger, no marker, no artifact;
4. open the frozen single-writer CE ledger with the G10E freeze ID;
5. reserve the authorized plan maximum for this arm: the request-level
   reservation always strictly precedes transport;
6. refuse before transport when the conservative per-request CE bound exceeds the
   authorized plan maximum -- the reservation is released with ``send_started``
   still False, no journal is written, and zero transport happens;
7. persist ``send_started``, then persist the single-use token consumption
   marker;
8. make exactly one application-visible transport attempt inside the production
   paid-request journal with client ``max_retries=0``;
9. accept the response only with exact model identity and complete usage, compute
   the exact accepted CE from the frozen Decimal ratecard, settle the reservation,
   and persist the accepted artifact atomically.

When the durable journal already holds a ``response_recorded`` response for
exactly this request and the durable ledger holds its accepted reservation, the
invocation is a local-only REPLAY: zero transport (a deliberately poisoned send
path proves it) and the identical artifact is rewritten.

There is no automatic loop, no automatic retry, no model substitution, and never
a second send for an ambiguous remote outcome. An ambiguous or unusable outcome
keeps the full reservation, durably quarantines exactly that
model+novel+chapter+case_kind, and leaves the paired partner case executable.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from benchmark_longitudinal_g10_matched import (
    G10A2_CONTRACT_SHA256,
    G10A2_OPERATION_KIND,
    G10A2_REQUEST_COUNT,
    build_runtime_cases,
    matched_plan_identity,
)
from benchmark_longitudinal_g10_paid_authorization import (
    EXPECTED_FREEZE_FILE_SHA256,
    EXPECTED_FREEZE_ID,
    EXPECTED_LEDGER_SHA256,
    EXPECTED_MATCHED_PLAN_SHA256,
    EXPECTED_PRICING_VERSION,
    EXPECTED_S0_MANIFEST_SHA256,
    G10PAID_AUTHORIZATION_SHA256,
    G10PAID_REPOSITORY_ANCHOR,
    G10PAID_REVISION,
    G10PAID_SCHEMA_VERSION,
    G10PaidAuthorizationError,
    G10PaidBudgetError,
    G10PaidQuarantineError,
    LITERARY_CASE_QUARANTINE_SCOPE,
    MODEL_RATECARD,
    accepted_credit_equivalent,
    assert_operator_authorization_present,
    authorization_consumption_path,
    build_paid_authorization,
    frozen_execution_binding,
    load_paid_quarantine,
    mark_send_started,
    open_frozen_budget_ledger,
    paid_artifact_dir,
    preflight_and_reserve,
    quarantine_literary_case,
    record_authorization_consumption,
    reservation_record,
    settle_accepted,
    settle_ambiguous,
    settle_pre_send_release,
    target_state_fingerprint,
    validate_execution_binding,
    verify_paid_authorization,
)
from benchmark_longitudinal_g10_safety import (
    G10ReplayError,
    assert_local_replay_preconditions,
)
from benchmark_longitudinal_provider import (
    accept_provider_response,
    build_provider_request_identity,
    create_provider_client,
    normalize_provider_response,
    send_chat_completion_once,
)
from noveltranslator_core import atomic_write_json
from production_paid_request_journal import (
    load_matching_journal,
    run_journaled_paid_request,
)


G10F_SCHEMA_VERSION = 1
G10F_REVISION = "g10-matched-paid-executor-v1"
G10F_REPOSITORY_ANCHOR = G10PAID_REPOSITORY_ANCHOR
G10F_ARM_ID = "G10A"
G10F_MIN_EXECUTION_INDEX = 1
G10F_MAX_EXECUTION_INDEX = G10A2_REQUEST_COUNT
PROMPT_TOKEN_SAFETY_OVERHEAD = 8192
G10F_QUARANTINE_DIRNAME = "quarantine"

# Operational paths that never represent "unchanged target state".
G10F_EVIDENCE_EXCLUDED_PREFIXES = (
    "journal/",
    "artifacts/",
    "authorization/",
    f"{G10F_QUARANTINE_DIRNAME}/",
)
G10F_EVIDENCE_EXCLUDED_FILES: tuple[str, ...] = ()


class G10FExecutionError(RuntimeError):
    """G10F state, authorization, accounting, or execution is unsafe."""


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise G10FExecutionError(
            "G10F value is not canonically JSON-serializable"
        ) from exc


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _require_sha256(value: Any, label: str) -> str:
    review = copy.deepcopy(value)
    if (
        not isinstance(review, str)
        or len(review) != 64
        or any(ch not in "0123456789abcdef" for ch in review)
    ):
        raise G10FExecutionError(f"{label} must be a lowercase SHA-256")
    return review


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise G10FExecutionError(f"{label} must be a positive integer")
    return value


def _positive_decimal(value: Any, label: str) -> Decimal:
    if isinstance(value, bool):
        raise G10FExecutionError(
            f"{label} must be a positive finite decimal"
        )
    try:
        parsed = Decimal(str(value))
    except Exception as exc:  # noqa: BLE001 - Decimal raises several types
        raise G10FExecutionError(
            f"{label} must be a positive finite decimal"
        ) from exc
    if not parsed.is_finite() or parsed <= 0:
        raise G10FExecutionError(
            f"{label} must be a positive finite decimal"
        )
    return parsed


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _assert_no_symlinks(root: Path) -> None:
    if root.is_symlink():
        raise G10FExecutionError(
            f"G10F evidence root must not be a symlink: {root}"
        )
    for item in root.rglob("*"):
        if item.is_symlink():
            raise G10FExecutionError(
                f"G10F evidence tree contains a symlink: {item}"
            )


def _aware_utc_iso(clock: Any) -> str:
    value = (clock or (lambda: datetime.now(timezone.utc)))()
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise G10FExecutionError(
            "G10F clock must return a timezone-aware datetime"
        )
    return value.astimezone(timezone.utc).isoformat()


def g10f_executor_identity() -> dict[str, Any]:
    """Return the deterministic zero-Provider G10F executor contract."""
    return {
        "schema_version": G10F_SCHEMA_VERSION,
        "revision": G10F_REVISION,
        "repository_anchor": G10F_REPOSITORY_ANCHOR,
        "arm_id": G10F_ARM_ID,
        "predecessor": {
            "g10_paid_authorization_sha256": G10PAID_AUTHORIZATION_SHA256,
            "g10_paid_authorization_revision": G10PAID_REVISION,
            "g10a2_matched_contract_sha256": G10A2_CONTRACT_SHA256,
            "matched_plan_sha256": EXPECTED_MATCHED_PLAN_SHA256,
            "freeze_id": EXPECTED_FREEZE_ID,
            "freeze_file_sha256": EXPECTED_FREEZE_FILE_SHA256,
            "budget_ledger_sha256": EXPECTED_LEDGER_SHA256,
            "s0_manifest_sha256": EXPECTED_S0_MANIFEST_SHA256,
            "pricing_version": EXPECTED_PRICING_VERSION,
        },
        "execution_scope": {
            "one_execution_index_per_invocation": True,
            "execution_index_range": [
                G10F_MIN_EXECUTION_INDEX,
                G10F_MAX_EXECUTION_INDEX,
            ],
            "automatic_loop": False,
            "automatic_retry": False,
            "one_application_visible_transport_attempt": True,
            "provider_client_max_retries": 0,
            "production_lifecycle_used": False,
            "state_free_literary_case": True,
            "journal_owner": "production_paid_request_journal",
        },
        "accounting": {
            "accepted_ce_from_frozen_ratecard_only": True,
            "conversion_requires_exact_model_and_complete_usage": True,
            "reservation_before_transport": True,
            "send_started_persisted_before_transport": True,
            "ambiguity_keeps_full_reservation": True,
        },
        "quarantine": {
            "scope_kind": LITERARY_CASE_QUARANTINE_SCOPE,
            "scope": "model+novel+chapter+case_kind",
            "create_once_never_overwrite": True,
            "paired_partner_case_remains_executable": True,
        },
        "artifact": {
            "accepted_artifact_required": True,
            "atomic_write_required": True,
            "idempotent_rewrite": True,
        },
        "replay": {
            "durable_response_recorded_required": True,
            "accepted_ledger_record_required": True,
            "poisoned_transport_required": True,
            "new_provider_send_forbidden": True,
        },
        "provider_calls": 0,
        "paid_execution_authorized": False,
    }


# The executor identity above binds every frozen predecessor identity by
# literal, so the module-level SHA-256 is deterministic and zero-Provider.
G10F_EXECUTOR_SHA256 = _sha256_json(g10f_executor_identity())


def g10f_target_state_fingerprint(evidence_root: Path | str) -> str:
    """Fingerprint the target state of one matched-literary evidence root."""
    return target_state_fingerprint(
        evidence_root,
        excluded_prefixes=G10F_EVIDENCE_EXCLUDED_PREFIXES,
        excluded_files=G10F_EVIDENCE_EXCLUDED_FILES,
    )


def conservative_credit_equivalent_bound(
    model_id: str,
    request_body: Mapping[str, Any],
) -> Decimal:
    """Return the conservative per-request CE upper bound for one request."""
    if model_id not in MODEL_RATECARD:
        raise G10FExecutionError(
            f"unknown G10 ratecard model: {model_id!r}"
        )
    if not isinstance(request_body, Mapping):
        raise G10FExecutionError("provider request body must be a mapping")
    maximum_output = _positive_int(
        request_body.get("max_completion_tokens"),
        "provider max_completion_tokens",
    )
    serialized = len(_canonical_json(dict(request_body)).encode("utf-8"))
    prompt_upper = serialized + PROMPT_TOKEN_SAFETY_OVERHEAD
    row = MODEL_RATECARD[model_id]
    return (
        Decimal(prompt_upper) * Decimal(str(row["input"]))
        + Decimal(maximum_output) * Decimal(str(row["output"]))
    ) / Decimal(1_000_000)


def _quarantine_path(
    evidence_root: Path | str,
    *,
    model_id: str,
    novel_id: str,
    chapter: int,
    case_kind: str,
) -> Path:
    return (
        Path(evidence_root)
        / G10F_QUARANTINE_DIRNAME
        / novel_id
        / _model_slug(model_id)
        / f"{chapter:04d}.{case_kind}.json"
    )


def _model_slug(model_id: str) -> str:
    from benchmark_longitudinal_g10_preregistration import (
        GLM_MODEL_ID,
        QWEN_MODEL_ID,
    )

    if model_id == QWEN_MODEL_ID:
        return "qwen"
    if model_id == GLM_MODEL_ID:
        return "glm"
    raise G10FExecutionError(
        f"unexpected G10A model for evidence layout: {model_id!r}"
    )


def materialize_g10f_case(
    *,
    repo_root: Path | str,
    evidence_root: Path | str,
    execution_index: int,
    prompt_bundles: Mapping[Any, Any] | None = None,
    forbidden_roots: tuple[Path | str, ...] = (),
) -> Any:
    """Re-materialize exactly one frozen matched case, never a loop."""
    index = _positive_int(execution_index, "execution_index")
    if not (
        G10F_MIN_EXECUTION_INDEX
        <= index
        <= G10F_MAX_EXECUTION_INDEX
    ):
        raise G10FExecutionError(
            "G10F invocation requires exactly one execution_index within "
            f"{G10F_MIN_EXECUTION_INDEX}..{G10F_MAX_EXECUTION_INDEX}"
        )
    cases = build_runtime_cases(
        repo_root=repo_root,
        evidence_root=evidence_root,
        prompt_bundles=prompt_bundles,
        forbidden_roots=forbidden_roots,
    )
    matches = [case for case in cases if case.execution_index == index]
    if len(matches) != 1:
        raise G10FExecutionError(
            "G10F execution index does not resolve uniquely"
        )
    return matches[0]


def _case_identity(case: Any) -> dict[str, Any]:
    return {
        "execution_index": case.execution_index,
        "pair_id": case.pair.pair_id,
        "pair_index": case.pair.pair_index,
        "case_kind": case.pair.case_kind,
        "novel_id": case.pair.novel_id,
        "chapter": case.pair.chapter,
        "model_id": case.benchmark_model_id,
        "provider_model_id": case.provider_model_id,
        "effort": case.effort,
        "request_sha256": case.provider_request_sha256,
        "journal_operation_id": case.journal_identity["operation_id"],
        "messages_sha256": case.prompt.messages_sha256,
        "text_sha256": case.prompt.text_sha256,
    }


def _assert_frozen_matched_plan(
    *,
    binding: Mapping[str, Any],
    repo_root: Path | str,
    evidence_root: Path | str,
    prompt_bundles: Mapping[Any, Any] | None,
    forbidden_roots: tuple[Path | str, ...],
) -> str:
    """Prove the live matched plan is exactly the frozen matched plan."""
    if prompt_bundles is not None:
        # An injected prompt bundle set exists only for hermetic tests; the
        # frozen plan SHA-256 is verified against the binding instead.
        frozen = binding.get("matched_plan_sha256")
        if frozen != EXPECTED_MATCHED_PLAN_SHA256:
            raise G10FExecutionError(
                "G10F evidence root binds another matched plan"
            )
        return frozen
    plan = matched_plan_identity(
        repo_root=repo_root,
        evidence_root=evidence_root,
        forbidden_roots=forbidden_roots,
    )
    matched_sha = _sha256_json(plan)
    if matched_sha != EXPECTED_MATCHED_PLAN_SHA256:
        raise G10FExecutionError(
            "G10F matched plan SHA-256 differs from the frozen literal"
        )
    if binding.get("matched_plan_sha256") != matched_sha:
        raise G10FExecutionError(
            "G10F binding binds another matched plan"
        )
    return matched_sha


def build_g10f_invocation(
    *,
    binding: Mapping[str, Any],
    repo_root: Path | str,
    evidence_root: Path | str,
    execution_index: int,
    model_id: str,
    target_state_fingerprint_sha256: str,
    maximum_credit_equivalent: Any,
    prompt_bundles: Mapping[Any, Any] | None = None,
    forbidden_roots: tuple[Path | str, ...] = (),
) -> dict[str, Any]:
    """Build one zero-Provider G10F invocation plan for exactly one case."""
    binding = validate_execution_binding(binding)
    evidence = Path(evidence_root).resolve()
    if not evidence.is_dir():
        raise G10FExecutionError(
            f"G10F evidence root is missing: {evidence}"
        )
    _assert_no_symlinks(evidence)
    if str(evidence) != binding.get("matched_evidence_root"):
        raise G10FExecutionError(
            "G10F evidence root is not the frozen matched evidence root"
        )

    _assert_frozen_matched_plan(
        binding=binding,
        repo_root=repo_root,
        evidence_root=evidence,
        prompt_bundles=prompt_bundles,
        forbidden_roots=forbidden_roots,
    )
    case = materialize_g10f_case(
        repo_root=repo_root,
        evidence_root=evidence,
        execution_index=execution_index,
        prompt_bundles=prompt_bundles,
        forbidden_roots=forbidden_roots,
    )
    if case.benchmark_model_id != model_id:
        raise G10FExecutionError(
            "G10F invocation model is not the frozen case model"
        )
    journal_path = Path(case.journal_path).resolve()
    if not _is_within(journal_path, evidence):
        raise G10FExecutionError(
            "G10F case journal path escapes the evidence root"
        )

    authorization = build_paid_authorization(
        binding=binding,
        arm_id=G10F_ARM_ID,
        model_id=case.benchmark_model_id,
        provider_model_id=case.provider_model_id,
        logical_operation=G10A2_OPERATION_KIND,
        novel_id=case.pair.novel_id,
        chapter=case.pair.chapter,
        case_kind=case.pair.case_kind,
        execution_index=case.execution_index,
        request_sha256=case.provider_request_sha256,
        target_state_fingerprint_sha256=target_state_fingerprint_sha256,
        maximum_credit_equivalent=maximum_credit_equivalent,
        maximum_new_provider_sends=1,
    )

    plan = {
        "schema_version": G10F_SCHEMA_VERSION,
        "revision": G10F_REVISION,
        "g10f_executor_sha256": G10F_EXECUTOR_SHA256,
        "g10_paid_authorization_sha256": G10PAID_AUTHORIZATION_SHA256,
        "benchmark_id": binding["benchmark_id"],
        "arm_id": G10F_ARM_ID,
        "execution_index": case.execution_index,
        "pair_id": case.pair.pair_id,
        "pair_index": case.pair.pair_index,
        "case_kind": case.pair.case_kind,
        "novel_id": case.pair.novel_id,
        "chapter": case.pair.chapter,
        "model_id": case.benchmark_model_id,
        "provider_model_id": case.provider_model_id,
        "effort": case.effort,
        "request_sha256": case.provider_request_sha256,
        "journal_operation_id": case.journal_identity["operation_id"],
        "journal_relative_path": journal_path.relative_to(
            evidence
        ).as_posix(),
        "target_state_fingerprint_sha256": (
            authorization["target_state_fingerprint_sha256"]
        ),
        "maximum_credit_equivalent": authorization[
            "maximum_credit_equivalent"
        ],
        "maximum_new_provider_sends": authorization[
            "maximum_new_provider_sends"
        ],
        "freeze_id": binding["freeze_id"],
        "freeze_file_sha256": binding["freeze_file_sha256"],
        "budget_ledger_sha256": binding["budget_ledger_sha256"],
        "matched_plan_sha256": binding["matched_plan_sha256"],
        "s0_manifest_sha256": binding["s0_manifest_sha256"],
        "pricing_version": binding["pricing_version"],
        "authorization": authorization,
        "approval_token": authorization["approval_token"],
        "production_lifecycle_used": False,
        "provider_calls": 0,
    }
    plan["plan_sha256"] = _sha256_json(plan)
    return plan


def _validate_plan_envelope(plan: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(plan, Mapping):
        raise G10FExecutionError("G10F invocation plan must be a mapping")
    value = copy.deepcopy(dict(plan))
    stored = value.pop("plan_sha256", None)
    _require_sha256(stored, "G10F invocation plan SHA")
    if _sha256_json(value) != stored:
        raise G10FExecutionError(
            "G10F invocation plan SHA does not recompute"
        )
    if value.get("schema_version") != G10F_SCHEMA_VERSION:
        raise G10FExecutionError("unsupported G10F invocation plan schema")
    if value.get("revision") != G10F_REVISION:
        raise G10FExecutionError("unsupported G10F invocation plan revision")
    if value.get("g10f_executor_sha256") != G10F_EXECUTOR_SHA256:
        raise G10FExecutionError(
            "invocation plan binds another G10F executor"
        )
    if value.get("g10_paid_authorization_sha256") != (
        G10PAID_AUTHORIZATION_SHA256
    ):
        raise G10FExecutionError(
            "invocation plan binds another G10F/G10G authorization contract"
        )
    if value.get("arm_id") != G10F_ARM_ID:
        raise G10FExecutionError("G10F plan binds another arm")
    if value.get("provider_calls") != 0:
        raise G10FExecutionError(
            "materialized G10F plan must report zero Provider calls"
        )
    if value.get("production_lifecycle_used") is not False:
        raise G10FExecutionError(
            "G10F matched-literary arm must not run the production lifecycle"
        )
    for field in (
        "freeze_id",
        "freeze_file_sha256",
        "budget_ledger_sha256",
        "matched_plan_sha256",
        "s0_manifest_sha256",
        "pricing_version",
    ):
        if not isinstance(value.get(field), str) or not value[field]:
            raise G10FExecutionError(
                f"G10F plan lacks its frozen binding field {field}"
            )
    nested = value.get("authorization")
    if not isinstance(nested, Mapping):
        raise G10FExecutionError(
            "G10F plan lacks its nested paid authorization"
        )
    if value.get("approval_token") != nested.get("approval_token"):
        raise G10FExecutionError(
            "G10F plan approval token binding changed"
        )
    _require_sha256(value.get("request_sha256"), "G10F request_sha256")
    _positive_int(value.get("execution_index"), "G10F execution_index")
    _positive_int(value.get("chapter"), "G10F chapter")
    _positive_decimal(
        value.get("maximum_credit_equivalent"),
        "G10F maximum_credit_equivalent",
    )
    _positive_int(
        value.get("maximum_new_provider_sends"),
        "G10F maximum_new_provider_sends",
    )
    if value["maximum_new_provider_sends"] != 1:
        raise G10FExecutionError(
            "G10F invocation authorizes exactly one transport attempt"
        )
    value["plan_sha256"] = stored
    return value


def _journal_path_for_plan(
    plan: Mapping[str, Any],
    evidence_root: Path,
) -> Path:
    relative = plan.get("journal_relative_path")
    if not isinstance(relative, str) or not relative:
        raise G10FExecutionError("G10F plan lacks its journal path")
    journal_path = (evidence_root / relative).resolve()
    if not _is_within(journal_path, evidence_root):
        raise G10FExecutionError(
            "G10F journal path escapes the evidence root"
        )
    return journal_path


def verify_g10f_invocation(
    *,
    plan: Mapping[str, Any],
    binding: Mapping[str, Any],
    repo_root: Path | str,
    evidence_root: Path | str,
    prompt_bundles: Mapping[Any, Any] | None = None,
    forbidden_roots: tuple[Path | str, ...] = (),
    expected_plan_sha256: str | None = None,
    expected_approval_token: str | None = None,
    operator_authorized: Any = None,
    current_state_fingerprint_sha256: str | None = None,
) -> dict[str, Any]:
    """Verify one G10F invocation exactly, before any side effect.

    ``operator_authorized=None`` performs the operator-independent verification
    only; the explicit operator gate is applied separately by
    :func:`execute_g10f_invocation` so that an unauthorized invocation can return
    a read-only ``AUTHORIZATION_REQUIRED`` report without touching a ledger or a
    client.
    """
    value = _validate_plan_envelope(plan)
    binding = validate_execution_binding(binding)
    for field in (
        "freeze_id",
        "freeze_file_sha256",
        "budget_ledger_sha256",
        "matched_plan_sha256",
        "s0_manifest_sha256",
        "pricing_version",
    ):
        if value.get(field) != binding[field]:
            raise G10FExecutionError(
                f"G10F invocation binding changed for {field}"
            )
    evidence = Path(evidence_root).resolve()
    if str(evidence) != binding.get("matched_evidence_root"):
        raise G10FExecutionError(
            "G10F evidence root is not the frozen matched evidence root"
        )

    if expected_plan_sha256 is not None:
        if value["plan_sha256"] != _require_sha256(
            expected_plan_sha256, "expected_plan_sha256"
        ):
            raise G10FExecutionError(
                "G10F invocation plan SHA mismatch"
            )
    if expected_approval_token is not None:
        if value["approval_token"] != _require_sha256(
            expected_approval_token, "expected_approval_token"
        ):
            raise G10FExecutionError(
                "G10F invocation approval token mismatch"
            )

    _assert_frozen_matched_plan(
        binding=binding,
        repo_root=repo_root,
        evidence_root=evidence,
        prompt_bundles=prompt_bundles,
        forbidden_roots=forbidden_roots,
    )
    case = materialize_g10f_case(
        repo_root=repo_root,
        evidence_root=evidence,
        execution_index=value["execution_index"],
        prompt_bundles=prompt_bundles,
        forbidden_roots=forbidden_roots,
    )
    identity = _case_identity(case)
    for field in (
        "pair_id",
        "pair_index",
        "case_kind",
        "novel_id",
        "chapter",
        "model_id",
        "provider_model_id",
        "effort",
        "request_sha256",
        "journal_operation_id",
    ):
        if value.get(field) != identity[field]:
            raise G10FExecutionError(
                f"G10F invocation case identity changed for {field}"
            )
    request_identity = build_provider_request_identity(
        request_body=case.request_body
    )
    if request_identity.request_sha256 != value["request_sha256"]:
        raise G10FExecutionError(
            "G10F re-materialized request body SHA differs from the plan"
        )
    journal_path = _journal_path_for_plan(value, evidence)
    if journal_path.name != Path(case.journal_path).name:
        raise G10FExecutionError("G10F case journal slot changed")

    fingerprint = (
        target_state_fingerprint(
            evidence,
            excluded_prefixes=G10F_EVIDENCE_EXCLUDED_PREFIXES,
            excluded_files=G10F_EVIDENCE_EXCLUDED_FILES,
        )
        if current_state_fingerprint_sha256 is None
        else _require_sha256(
            current_state_fingerprint_sha256,
            "current_state_fingerprint_sha256",
        )
    )

    existing = load_matching_journal(journal_path, case.journal_identity)
    journal_status = None if existing is None else existing["status"]

    consumption_path = authorization_consumption_path(
        evidence, value["approval_token"]
    )
    verified_authorization = verify_paid_authorization(
        plan=value["authorization"],
        expected_approval_token=value["approval_token"],
        current_state_fingerprint_sha256=fingerprint,
        operator_authorized=(
            True if operator_authorized is None else operator_authorized
        ),
        binding=binding,
        consumption_path=(
            consumption_path
            if journal_status in {None, "planned"}
            else None
        ),
    )

    return {
        "plan": value,
        "plan_sha256": value["plan_sha256"],
        "approval_token": value["approval_token"],
        "case": case,
        "case_identity": identity,
        "journal_path": journal_path,
        "journal_status": journal_status,
        "journal_record": existing,
        "current_state_fingerprint_sha256": fingerprint,
        "consumption_path": consumption_path,
        "authorization": verified_authorization,
        "request_identity": request_identity,
        "provider_calls": 0,
    }


def _poisoned_transport(*_args: Any, **_kwargs: Any) -> Any:
    raise G10FExecutionError(
        "G10F replay transport is deliberately poisoned; no send is authorized"
    )


def _usage_identity(usage: Any) -> dict[str, Any]:
    return {
        "prompt_tokens": getattr(usage, "prompt_tokens", None),
        "completion_tokens": getattr(usage, "completion_tokens", None),
        "cached_tokens": getattr(usage, "cached_tokens", None),
        "cached_tokens_exposed": bool(
            getattr(usage, "cached_tokens_exposed", False)
        ),
        "total_tokens": getattr(usage, "total_tokens", None),
        "reasoning_tokens": getattr(usage, "reasoning_tokens", None),
        "reasoning_tokens_exposed": bool(
            getattr(usage, "reasoning_tokens_exposed", False)
        ),
    }


def _artifact_path(evidence_root: Path, case: Any) -> Path:
    return (
        paid_artifact_dir(
            evidence_root,
            case.pair.novel_id,
            _model_slug(case.benchmark_model_id),
        )
        / f"{case.pair.chapter:04d}.{case.pair.case_kind}.json"
    )


def _build_artifact(
    *,
    verified: Mapping[str, Any],
    reservation_id: str,
    accepted_ce: str,
    usage: Mapping[str, Any],
    content: Any,
    finish_reason: Any,
    provider_response_id: Any,
    provider_response_model: Any,
) -> dict[str, Any]:
    plan = verified["plan"]
    identity = verified["case_identity"]
    return {
        "schema_version": G10F_SCHEMA_VERSION,
        "revision": G10F_REVISION,
        "g10f_executor_sha256": G10F_EXECUTOR_SHA256,
        "benchmark_id": plan["benchmark_id"],
        "arm_id": G10F_ARM_ID,
        "freeze_id": plan["freeze_id"],
        "matched_plan_sha256": plan["matched_plan_sha256"],
        "pricing_version": plan["pricing_version"],
        "plan_sha256": plan["plan_sha256"],
        "approval_token": plan["approval_token"],
        "execution_index": plan["execution_index"],
        "pair_id": plan["pair_id"],
        "pair_index": plan["pair_index"],
        "case_kind": plan["case_kind"],
        "novel_id": plan["novel_id"],
        "chapter": plan["chapter"],
        "model_id": plan["model_id"],
        "provider_model_id": plan["provider_model_id"],
        "effort": plan["effort"],
        "request_sha256": plan["request_sha256"],
        "journal_operation_id": plan["journal_operation_id"],
        "journal_relative_path": plan["journal_relative_path"],
        "reservation_id": reservation_id,
        "accepted_credit_equivalent": accepted_ce,
        "maximum_credit_equivalent": plan["maximum_credit_equivalent"],
        "usage": copy.deepcopy(dict(usage)),
        "provider_response_id": provider_response_id,
        "provider_response_model": provider_response_model,
        "finish_reason": finish_reason,
        "content": content,
        "messages_sha256": identity["messages_sha256"],
        "text_sha256": identity["text_sha256"],
        "accepted_credit_equivalent_basis": (
            "accepted_provider_usage_and_frozen_decimal_ratecard"
        ),
        "provider_calls": 1,
    }


def _write_artifact(path: Path, artifact: Mapping[str, Any]) -> dict[str, Any]:
    # ``atomic_write_json`` creates an exclusive sibling temp file beside the
    # target, so the artifact directory must exist first.
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, copy.deepcopy(dict(artifact)))
    return copy.deepcopy(dict(artifact))


def _accepted_reservation_for(
    ledger: Any,
    request_sha256: str,
) -> dict[str, Any]:
    matches = [
        record
        for record in ledger.document["records"]
        if record.get("request_sha256") == request_sha256
    ]
    if len(matches) != 1:
        raise G10PaidBudgetError(
            "G10F request does not resolve to exactly one reservation"
        )
    return matches[0]


def _quarantine_literary_case(
    *,
    verified: Mapping[str, Any],
    evidence_root: Path,
    reservation_id: str,
    reason: str,
    outcome: str,
    clock: Any,
) -> dict[str, Any]:
    """Create the one durable quarantine marker for this case, or re-read it.

    The marker is create-once. A second invocation that finds the identical
    durable marker binds it rather than overwriting or duplicating it, so an
    already-quarantined case stays quarantined with zero transport.
    """
    plan = verified["plan"]
    path = _quarantine_path(
        evidence_root,
        model_id=plan["model_id"],
        novel_id=plan["novel_id"],
        chapter=plan["chapter"],
        case_kind=plan["case_kind"],
    )
    expected_scope = (
        f"{plan['model_id']}::{plan['novel_id']}::"
        f"{plan['chapter']:04d}::{plan['case_kind']}"
    )
    existing = load_paid_quarantine(path)
    if existing is not None:
        if (
            existing.get("scope_kind") != LITERARY_CASE_QUARANTINE_SCOPE
            or existing.get("scope_id") != expected_scope
            or existing.get("request_sha256") != plan["request_sha256"]
        ):
            raise G10PaidQuarantineError(
                "durable G10F quarantine marker binds another case"
            )
        return {**copy.deepcopy(existing), "already_present": True}
    marker = quarantine_literary_case(
        path=path,
        model_id=plan["model_id"],
        novel_id=plan["novel_id"],
        chapter=plan["chapter"],
        case_kind=plan["case_kind"],
        request_sha256=plan["request_sha256"],
        reservation_id=reservation_id,
        reason=reason,
        outcome=outcome,
        clock=clock,
    )
    return {**marker, "already_present": False}


def _pre_send_ceiling_refusal(
    *,
    verified: Mapping[str, Any],
    ledger: Any,
    reservation_id: str,
    bound: str,
) -> dict[str, Any]:
    """Report a refusal that provably happened strictly before transport.

    The reservation existed, then was released while ``send_started`` was still
    ``False``; no paid-request journal was created and zero transport happened.
    """
    plan = verified["plan"]
    return {
        "status": "CEILING_REJECTED_PRE_SEND",
        "arm_id": G10F_ARM_ID,
        "execution_index": plan["execution_index"],
        "pair_id": plan["pair_id"],
        "case_kind": plan["case_kind"],
        "model_id": plan["model_id"],
        "novel_id": plan["novel_id"],
        "chapter": plan["chapter"],
        "plan_sha256": plan["plan_sha256"],
        "approval_token": plan["approval_token"],
        "request_sha256": plan["request_sha256"],
        "journal_status": verified["journal_status"],
        "reservation_id": reservation_id,
        "reservation_status": "released_pre_send",
        "conservative_credit_equivalent_bound": bound,
        "maximum_credit_equivalent": plan["maximum_credit_equivalent"],
        "committed_credit_equivalent": _decimal_text(
            ledger.committed_credit_equivalent
        ),
        "remaining_credit_equivalent": _decimal_text(
            ledger.remaining_credit_equivalent
        ),
        "reason": (
            "G10F conservative credit-equivalent bound "
            f"{bound} exceeds the authorized plan maximum "
            f"{plan['maximum_credit_equivalent']}: the reservation was created "
            "and released pre-send, no journal was written, and no transport "
            "attempt was made"
        ),
        "provider_calls": 0,
        "provider_transport_attempts": 0,
        "paid_execution_authorized": True,
        "automatic_chapter_advance": False,
        "automatic_retry": False,
    }


def execute_g10f_invocation(
    *,
    code_root: Path | str,
    production_root: Path | str,
    freeze_root: Path | str,
    matched_evidence_root: Path | str,
    s0_root: Path | str,
    repo_root: Path | str,
    evidence_root: Path | str,
    plan: Mapping[str, Any],
    expected_plan_sha256: str,
    expected_approval_token: str,
    operator_authorized: Any,
    api_key: str | None = None,
    client_factory: Any = None,
    prompt_bundles: Mapping[Any, Any] | None = None,
    forbidden_roots: tuple[Path | str, ...] = (),
    clock: Any = None,
) -> dict[str, Any]:
    """Execute exactly one approved G10F matched-literary request.

    ``production_root`` is the read-only real NovelTranslator production root: the
    G10A arm never runs the production lifecycle and never mutates it.
    ``code_root`` is only used to prove the repository-local evidence layout is
    external to the code worktree; no production chapter is executed here.
    """
    code = Path(code_root).resolve()
    evidence = Path(evidence_root).resolve()
    if not code.is_dir():
        raise G10FExecutionError(f"G10F code root is missing: {code}")
    if not evidence.is_dir():
        raise G10FExecutionError(
            f"G10F evidence root is missing: {evidence}"
        )
    if _is_within(evidence, code) or _is_within(code, evidence):
        raise G10FExecutionError(
            "G10F evidence root must be external to the code worktree"
        )

    binding = frozen_execution_binding(
        production_root=production_root,
        freeze_root=freeze_root,
        matched_evidence_root=matched_evidence_root,
        s0_root=s0_root,
        forbidden_roots=forbidden_roots,
        repo_root=repo_root,
    )

    verified = verify_g10f_invocation(
        plan=plan,
        binding=binding,
        repo_root=repo_root,
        evidence_root=evidence,
        prompt_bundles=prompt_bundles,
        forbidden_roots=forbidden_roots,
        expected_plan_sha256=expected_plan_sha256,
        expected_approval_token=expected_approval_token,
        operator_authorized=None,
    )
    plan_value = verified["plan"]

    try:
        assert_operator_authorization_present(operator_authorized)
    except G10PaidAuthorizationError as exc:
        return {
            "status": "AUTHORIZATION_REQUIRED",
            "arm_id": G10F_ARM_ID,
            "execution_index": plan_value["execution_index"],
            "pair_id": plan_value["pair_id"],
            "case_kind": plan_value["case_kind"],
            "model_id": plan_value["model_id"],
            "novel_id": plan_value["novel_id"],
            "chapter": plan_value["chapter"],
            "plan_sha256": plan_value["plan_sha256"],
            "approval_token": plan_value["approval_token"],
            "journal_status": verified["journal_status"],
            "provider_calls": 0,
            "provider_transport_attempts": 0,
            "paid_execution_authorized": False,
            "reason": str(exc),
        }

    if not isinstance(api_key, str) or not api_key.strip():
        raise G10FExecutionError(
            "explicitly authorized G10F execution requires an injected API key"
        )

    case = verified["case"]
    journal_path = verified["journal_path"]
    token = plan_value["approval_token"]
    request_sha = plan_value["request_sha256"]
    ledger = open_frozen_budget_ledger(binding, clock=clock)

    if verified["journal_status"] == "response_recorded":
        return _replay_completed_case(
            verified=verified,
            ledger=ledger,
            evidence_root=evidence,
        )

    if verified["journal_status"] in {"started", "uncertain"}:
        return _resolve_ambiguous_case(
            verified=verified,
            ledger=ledger,
            evidence_root=evidence,
            clock=clock,
            reason=(
                "durable paid request journal already holds an unresolved "
                f"{verified['journal_status']} record; automatic reissue is "
                "forbidden"
            ),
        )

    # The client is constructed only after the exact plan/state verification and
    # the explicit operator gate both succeeded, and never when the arm is
    # unauthorized.
    provider_client = create_provider_client(
        api_key=api_key,
        client_factory=client_factory,
    )

    reservation = preflight_and_reserve(
        ledger=ledger,
        plan=plan_value["authorization"],
        request_sha256=request_sha,
        reserved_credit_equivalent=plan_value["maximum_credit_equivalent"],
    )
    reservation_id = reservation["reservation_id"]

    # The request-level reservation always precedes transport, and the single
    # send only exists when the conservative per-request CE bound is still
    # inside the authorized plan maximum. A refusal is therefore provably
    # pre-send: the reservation is released with ``send_started`` still False,
    # no journal is written, and zero transport happens.
    try:
        bound = conservative_credit_equivalent_bound(
            plan_value["model_id"], case.request_body
        )
    except G10FExecutionError:
        settle_pre_send_release(
            ledger=ledger,
            reservation_id=reservation_id,
            transport_never_started_proven=True,
        )
        raise
    if bound > Decimal(plan_value["maximum_credit_equivalent"]):
        settle_pre_send_release(
            ledger=ledger,
            reservation_id=reservation_id,
            transport_never_started_proven=True,
        )
        return _pre_send_ceiling_refusal(
            verified=verified,
            ledger=ledger,
            reservation_id=reservation_id,
            bound=_decimal_text(bound),
        )

    mark_send_started(ledger=ledger, reservation_id=reservation_id)
    record_authorization_consumption(
        path=verified["consumption_path"],
        approval_token=token,
        request_sha256=request_sha,
        reservation_id=reservation_id,
        clock=clock,
    )

    attempt_context = {
        "mode": G10F_REVISION,
        "arm_id": G10F_ARM_ID,
        "execution_index": plan_value["execution_index"],
        "pair_id": plan_value["pair_id"],
        "case_kind": plan_value["case_kind"],
        "chapter": plan_value["chapter"],
        "request_sha256": request_sha,
        "plan_sha256": plan_value["plan_sha256"],
        "pricing_version": plan_value["pricing_version"],
        "request_started_utc": _aware_utc_iso(clock),
    }
    provider_request_identity = verified["request_identity"]
    box: dict[str, Any] = {}

    def prepare_attempt() -> dict[str, Any]:
        return copy.deepcopy(attempt_context)

    def normalize_response(
        raw_response: Any,
        _attempt_metadata: Mapping[str, Any],
    ) -> dict[str, Any]:
        normalized = normalize_provider_response(
            raw_response,
            request_identity=provider_request_identity,
        )
        accepted = accept_provider_response(
            provider_request_identity,
            normalized,
        )
        exact_ce = accepted_credit_equivalent(
            plan_value["model_id"], accepted.usage
        )
        usage = _usage_identity(accepted.usage)
        envelope = {
            "content": accepted.content,
            "finish_reason": accepted.finish_reason,
            "provider_response_id": accepted.provider_response_id,
            "provider_response_model": accepted.response_model,
            "request_sha256": provider_request_identity.request_sha256,
            "logical_operation": G10A2_OPERATION_KIND,
            "arm_id": G10F_ARM_ID,
            "execution_index": plan_value["execution_index"],
            "model_id": plan_value["model_id"],
            "reservation_id": reservation_id,
            "accepted_credit_equivalent": _decimal_text(exact_ce),
            "usage": usage,
        }
        box.clear()
        box.update(envelope)
        return envelope

    try:
        result = run_journaled_paid_request(
            journal_path=journal_path,
            identity=case.journal_identity,
            save_json=lambda path, value: atomic_write_json(path, value),
            prepare_attempt=prepare_attempt,
            send_once=lambda: send_chat_completion_once(
                provider_client,
                case.request_body,
            ),
            normalize_response=normalize_response,
        )
    except BaseException as exc:  # noqa: BLE001 - ambiguity must be durable
        record = reservation_record(ledger, reservation_id)
        if record["send_started"]:
            return _resolve_ambiguous_case(
                verified=verified,
                ledger=ledger,
                evidence_root=evidence,
                clock=clock,
                reservation_id=reservation_id,
                transport_attempts=1,
                reason=(
                    "G10F paid request did not reach a durable response: "
                    f"{type(exc).__name__}: {str(exc)[:400]}"
                ),
            )
        settle_pre_send_release(
            ledger=ledger,
            reservation_id=reservation_id,
            transport_never_started_proven=True,
        )
        raise

    if not isinstance(result, Mapping) or result.get("api_called") is not True:
        raise G10FExecutionError(
            "G10F paid journal did not report exactly one transport attempt"
        )
    response = result.get("response")
    if not isinstance(response, Mapping) or not box:
        raise G10FExecutionError(
            "G10F paid journal returned no accepted response envelope"
        )
    if response != box:
        raise G10FExecutionError(
            "G10F journaled envelope differs from the accepted envelope"
        )

    try:
        settled = settle_accepted(
            ledger=ledger,
            reservation_id=reservation_id,
            accepted_credit_equivalent=response["accepted_credit_equivalent"],
        )
    except G10PaidBudgetError as exc:
        artifact = None
        return {
            "status": "QUARANTINED",
            "arm_id": G10F_ARM_ID,
            "execution_index": plan_value["execution_index"],
            "pair_id": plan_value["pair_id"],
            "case_kind": plan_value["case_kind"],
            "model_id": plan_value["model_id"],
            "novel_id": plan_value["novel_id"],
            "chapter": plan_value["chapter"],
            "plan_sha256": plan_value["plan_sha256"],
            "approval_token": token,
            "reservation_id": reservation_id,
            "quarantine": _quarantine_literary_case(
                verified=verified,
                evidence_root=evidence,
                reservation_id=reservation_id,
                reason=(
                    "accepted Provider usage exceeded the conservative "
                    f"reservation: {exc}"
                ),
                outcome="accepted_budget_boundary_corrupt",
                clock=clock,
            ),
            "terminal_budget_overrun": bool(
                ledger.has_terminal_budget_overrun
            ),
            "provider_calls": 1,
            "provider_transport_attempts": 1,
            "accepted_artifact": artifact,
            "accepted_credit_equivalent": response[
                "accepted_credit_equivalent"
            ],
            "paid_execution_authorized": True,
        }

    artifact = _write_artifact(
        _artifact_path(evidence, case),
        _build_artifact(
            verified=verified,
            reservation_id=reservation_id,
            accepted_ce=response["accepted_credit_equivalent"],
            usage=response["usage"],
            content=response["content"],
            finish_reason=response["finish_reason"],
            provider_response_id=response["provider_response_id"],
            provider_response_model=response["provider_response_model"],
        ),
    )
    return {
        "status": "COMPLETED",
        "arm_id": G10F_ARM_ID,
        "execution_index": plan_value["execution_index"],
        "pair_id": plan_value["pair_id"],
        "case_kind": plan_value["case_kind"],
        "model_id": plan_value["model_id"],
        "novel_id": plan_value["novel_id"],
        "chapter": plan_value["chapter"],
        "plan_sha256": plan_value["plan_sha256"],
        "approval_token": token,
        "request_sha256": request_sha,
        "journal_status": "response_recorded",
        "reservation_id": reservation_id,
        "accepted_credit_equivalent": response["accepted_credit_equivalent"],
        "committed_credit_equivalent": settled[
            "committed_credit_equivalent"
        ],
        "remaining_credit_equivalent": settled[
            "remaining_credit_equivalent"
        ],
        "accepted_artifact": str(_artifact_path(evidence, case)),
        "accepted_artifact_identity": artifact,
        "provider_calls": 1,
        "provider_transport_attempts": 1,
        "paid_execution_authorized": True,
        "automatic_chapter_advance": False,
        "automatic_retry": False,
    }


def _resolve_ambiguous_case(
    *,
    verified: Mapping[str, Any],
    ledger: Any,
    evidence_root: Path,
    clock: Any,
    reason: str,
    reservation_id: str | None = None,
    transport_attempts: int = 0,
) -> dict[str, Any]:
    """Keep the full reservation and quarantine exactly this literary case."""
    plan = verified["plan"]
    request_sha = plan["request_sha256"]
    if reservation_id is None:
        record = _accepted_reservation_for(ledger, request_sha)
        reservation_id = record["reservation_id"]
    record = reservation_record(ledger, reservation_id)
    if record["send_started"] and record["status"] == "reserved":
        settle_ambiguous(
            ledger=ledger,
            reservation_id=reservation_id,
            reason=reason,
        )
    elif record["status"] not in {"ambiguous", "reserved"}:
        reason = f"{reason}; durable reservation status={record['status']}"
    marker = _quarantine_literary_case(
        verified=verified,
        evidence_root=evidence_root,
        reservation_id=reservation_id,
        reason=reason,
        outcome="ambiguous_remote_outcome",
        clock=clock,
    )
    return {
        "status": "QUARANTINED",
        "arm_id": G10F_ARM_ID,
        "execution_index": plan["execution_index"],
        "pair_id": plan["pair_id"],
        "case_kind": plan["case_kind"],
        "model_id": plan["model_id"],
        "novel_id": plan["novel_id"],
        "chapter": plan["chapter"],
        "plan_sha256": plan["plan_sha256"],
        "approval_token": plan["approval_token"],
        "request_sha256": request_sha,
        "reservation_id": reservation_id,
        "reservation_status": reservation_record(ledger, reservation_id)[
            "status"
        ],
        "quarantine": marker,
        "full_reservation_retained": True,
        "committed_credit_equivalent": _decimal_text(
            ledger.committed_credit_equivalent
        ),
        "provider_calls": int(transport_attempts),
        "provider_transport_attempts": int(transport_attempts),
        "paid_execution_authorized": True,
        "automatic_chapter_advance": False,
        "automatic_retry": False,
    }


def _replay_completed_case(
    *,
    verified: Mapping[str, Any],
    ledger: Any,
    evidence_root: Path,
) -> dict[str, Any]:
    """Replay one durable accepted response with zero transport."""
    plan = verified["plan"]
    case = verified["case"]
    record = _accepted_reservation_for(ledger, plan["request_sha256"])
    if record["status"] != "accepted" or not record["send_started"]:
        raise G10PaidBudgetError(
            "G10F replay requires exactly one accepted started reservation"
        )
    try:
        replay_gate = assert_local_replay_preconditions(
            journal_status="response_recorded",
            ledger_status="accepted",
            exact_request_match=True,
            transport_poisoned=True,
        )
    except G10ReplayError as exc:
        raise G10FExecutionError(
            f"G10F replay preconditions are not proven: {exc}"
        ) from exc

    result = run_journaled_paid_request(
        journal_path=verified["journal_path"],
        identity=case.journal_identity,
        save_json=lambda path, value: atomic_write_json(path, value),
        prepare_attempt=lambda: _poisoned_transport(),
        send_once=lambda: _poisoned_transport(),
        normalize_response=lambda *_args, **_kwargs: _poisoned_transport(),
    )
    if result.get("replayed") is not True or result.get("api_called") is not False:
        raise G10FExecutionError(
            "G10F replay unexpectedly attempted provider transport"
        )
    response = result.get("response")
    if not isinstance(response, Mapping):
        raise G10FExecutionError(
            "G10F replayed journal holds no durable response envelope"
        )
    if response.get("accepted_credit_equivalent") != _decimal_text(
        Decimal(str(record["accepted_credit_equivalent"]))
    ):
        raise G10FExecutionError(
            "G10F replayed response accounting differs from the durable ledger"
        )
    artifact = _write_artifact(
        _artifact_path(evidence_root, case),
        _build_artifact(
            verified=verified,
            reservation_id=record["reservation_id"],
            accepted_ce=response["accepted_credit_equivalent"],
            usage=response["usage"],
            content=response["content"],
            finish_reason=response["finish_reason"],
            provider_response_id=response["provider_response_id"],
            provider_response_model=response["provider_response_model"],
        ),
    )
    return {
        "status": "REPLAYED",
        "arm_id": G10F_ARM_ID,
        "execution_index": plan["execution_index"],
        "pair_id": plan["pair_id"],
        "case_kind": plan["case_kind"],
        "model_id": plan["model_id"],
        "novel_id": plan["novel_id"],
        "chapter": plan["chapter"],
        "plan_sha256": plan["plan_sha256"],
        "approval_token": plan["approval_token"],
        "request_sha256": plan["request_sha256"],
        "journal_status": "response_recorded",
        "reservation_id": record["reservation_id"],
        "accepted_credit_equivalent": response[
            "accepted_credit_equivalent"
        ],
        "replay_gate": replay_gate,
        "accepted_artifact": str(_artifact_path(evidence_root, case)),
        "accepted_artifact_identity": artifact,
        "provider_calls": 0,
        "provider_transport_attempts": 0,
        "paid_execution_authorized": True,
        "new_provider_send_authorized": False,
    }


__all__ = [
    "G10F_ARM_ID",
    "G10F_EVIDENCE_EXCLUDED_FILES",
    "G10F_EVIDENCE_EXCLUDED_PREFIXES",
    "G10F_EXECUTOR_SHA256",
    "G10F_MAX_EXECUTION_INDEX",
    "G10F_MIN_EXECUTION_INDEX",
    "G10F_REVISION",
    "G10F_SCHEMA_VERSION",
    "G10FExecutionError",
    "PROMPT_TOKEN_SAFETY_OVERHEAD",
    "build_g10f_invocation",
    "conservative_credit_equivalent_bound",
    "execute_g10f_invocation",
    "g10f_executor_identity",
    "g10f_target_state_fingerprint",
    "materialize_g10f_case",
    "verify_g10f_invocation",
]
