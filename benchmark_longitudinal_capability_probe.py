"""Journal-protected G5C direct-Provider capability probes.

This module is intentionally separate from literary translation. It verifies the
smallest direct Command Code Provider boundary needed before G6:

* canonical Provider model spelling is accepted;
* the Provider returns that exact model identity;
* prompt/completion usage is present;
* the statically discovered reasoning_effort field accepts the frozen level for
  each adjustable model;
* Tencent Hy3 Paid and MiMo V2.5 Pro are probed only at provider-native/default
  effort.

The ten-case plan comes from benchmark_longitudinal_capabilities. Requests are
non-literary, sequential, non-streaming, and executed underneath the existing
production_paid_request_journal boundary. A network/transport ambiguity becomes
uncertain and blocks automatic reissue. A definitive HTTP response, including a
4xx, is recorded durably and never auto-reissued.

Importing this module performs no network activity, reads no credentials, and
writes no files. Runtime execution reads only COMMAND_CODE_API_KEY; it never
loads .env or Command Code auth files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from benchmark_longitudinal_capabilities import (
    BENCHMARK_ZDR_ENABLED,
    CAPABILITY_FREEZE_SHA256,
    G5C_CAPABILITY_PROBE_CASES,
    G5C_CAPABILITY_PROBE_CALL_COUNT,
    G5C_PROBE_REVISION,
    REASONING_WIRE_FIELD,
    CapabilityProbeCase,
)
from benchmark_longitudinal_config import CHAT_COMPLETIONS_PATH, PROVIDER_BASE_URL
from benchmark_longitudinal_provider import PROVIDER_NAME
from production_paid_request_journal import (
    PaidRequestUncertainError,
    load_matching_journal,
    paid_request_journal_path,
    request_identity,
    run_journaled_paid_request,
)


PROBE_SCHEMA_VERSION = 1
PROBE_EXECUTION_REVISION = "goat-g5c-capability-probe-execution-v1"
PROBE_OPERATION_KIND = "goat_capability_probe"
PROBE_NOVEL_ID = "goat_capability_probe"
PROBE_PROFILE_ID = "goat_capability_probe"
PROBE_API_KEY_ENV = "COMMAND_CODE_API_KEY"
PROBE_MAX_COMPLETION_TOKENS = 16
PROBE_TIMEOUT_SECONDS = 90

PROBE_PROMPT = (
    "Protocol capability probe. Reply with exactly the two uppercase letters OK "
    "and no other text."
)
PROBE_PROMPT_SHA256 = hashlib.sha256(PROBE_PROMPT.encode("utf-8")).hexdigest()
PROVIDER_CHAT_URL = PROVIDER_BASE_URL.rstrip("/") + CHAT_COMPLETIONS_PATH


class CapabilityProbeError(RuntimeError):
    """Base class for G5C local safety or provider-boundary failures."""


class CapabilityProbeAuthorizationError(CapabilityProbeError):
    """The operator did not authorize the exact remaining provider-call count."""


class CapabilityProbeRejectedError(CapabilityProbeError):
    """A durable definitive response failed capability acceptance."""


class CapabilityProbePlanError(CapabilityProbeError):
    """Existing evidence does not match the frozen probe plan."""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(
        dict(value),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, path)


def build_probe_request(case: CapabilityProbeCase) -> dict[str, Any]:
    """Build exactly one frozen, non-literary G5C request body."""
    if not isinstance(case, CapabilityProbeCase):
        raise CapabilityProbePlanError("probe case must be a CapabilityProbeCase")
    if case.zdr_enabled or BENCHMARK_ZDR_ENABLED:
        raise CapabilityProbePlanError(
            "G5C freezes ZDR disabled for cross-model routing symmetry"
        )

    body: dict[str, Any] = {
        "model": case.provider_model_id,
        "messages": [
            {
                "role": "user",
                "content": PROBE_PROMPT,
            }
        ],
        "max_completion_tokens": PROBE_MAX_COMPLETION_TOKENS,
    }
    if case.effort is not None:
        body[REASONING_WIRE_FIELD] = case.effort
    return body


@dataclass(frozen=True)
class ProbeCaseRuntime:
    index: int
    case: CapabilityProbeCase
    request_body: dict[str, Any]
    identity: dict[str, Any]
    journal_path: Path

    def plan_identity(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "case": self.case.identity(),
            "request_body": self.request_body,
            "request_sha256": self.identity["request_sha256"],
            "operation_id": self.identity["operation_id"],
            "journal_file": self.journal_path.name,
        }


def build_probe_runtime_cases(evidence_root: Path | str) -> tuple[ProbeCaseRuntime, ...]:
    root = Path(evidence_root)
    journal_dir = root / "journal"
    runtime_cases: list[ProbeCaseRuntime] = []

    for index, case in enumerate(G5C_CAPABILITY_PROBE_CASES, start=1):
        request_body = build_probe_request(case)
        identity = request_identity(
            operation_kind=PROBE_OPERATION_KIND,
            novel_id=PROBE_NOVEL_ID,
            chapter=index,
            provider=PROVIDER_NAME,
            model=case.provider_model_id,
            profile_id=PROBE_PROFILE_ID,
            source_sha256=PROBE_PROMPT_SHA256,
            request_payload=request_body,
        )
        runtime_cases.append(
            ProbeCaseRuntime(
                index=index,
                case=case,
                request_body=request_body,
                identity=identity,
                journal_path=paid_request_journal_path(
                    journal_dir,
                    PROBE_OPERATION_KIND,
                    index,
                ),
            )
        )

    if len(runtime_cases) != G5C_CAPABILITY_PROBE_CALL_COUNT:
        raise CapabilityProbePlanError("frozen G5C probe count changed")
    return tuple(runtime_cases)


def probe_plan_identity(evidence_root: Path | str) -> dict[str, Any]:
    """Return a deterministic plan identity. Absolute root path is not hashed."""
    cases = build_probe_runtime_cases(evidence_root)
    return {
        "schema_version": PROBE_SCHEMA_VERSION,
        "execution_revision": PROBE_EXECUTION_REVISION,
        "probe_revision": G5C_PROBE_REVISION,
        "capability_freeze_sha256": CAPABILITY_FREEZE_SHA256,
        "provider_base_url": PROVIDER_BASE_URL,
        "endpoint_path": CHAT_COMPLETIONS_PATH,
        "reasoning_wire_field": REASONING_WIRE_FIELD,
        "zdr_enabled": False,
        "prompt_sha256": PROBE_PROMPT_SHA256,
        "max_completion_tokens": PROBE_MAX_COMPLETION_TOKENS,
        "case_count": len(cases),
        "cases": [item.plan_identity() for item in cases],
    }


def probe_plan_sha256(evidence_root: Path | str) -> str:
    return _sha256_json(probe_plan_identity(evidence_root))


def _response_header(headers: Any, name: str) -> str | None:
    if headers is None:
        return None
    try:
        value = headers.get(name)
    except Exception:
        return None
    return value if isinstance(value, str) and value else None


def _decode_http_body(raw: bytes) -> tuple[str, Any | None]:
    text = raw.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    return text, parsed


def send_probe_http_once(
    *,
    api_key: str,
    request_body: Mapping[str, Any],
    url: str = PROVIDER_CHAT_URL,
    timeout_seconds: int = PROBE_TIMEOUT_SECONDS,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, Any]:
    """Perform exactly one HTTP POST and capture any definitive HTTP response.

    HTTP status errors are returned as durable response data. Network/timeout
    errors propagate so the paid-request journal marks the outcome uncertain.
    """

    if not isinstance(api_key, str) or not api_key.strip():
        raise CapabilityProbeAuthorizationError(
            f"{PROBE_API_KEY_ENV} must contain a Command Code API key"
        )
    if url != PROVIDER_CHAT_URL:
        raise CapabilityProbePlanError("probe URL must be the exact Provider endpoint")
    if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool):
        raise CapabilityProbePlanError("probe timeout must be an integer")
    if timeout_seconds < 1:
        raise CapabilityProbePlanError("probe timeout must be positive")

    payload = _canonical_json(dict(request_body)).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "NovelTranslator-GOAT-G5C/1",
        },
    )

    try:
        response = opener(request, timeout=timeout_seconds)
        try:
            status = int(response.getcode())
            raw_body = response.read()
            headers = getattr(response, "headers", None)
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        raw_body = exc.read()
        headers = getattr(exc, "headers", None)

    text, parsed = _decode_http_body(raw_body)
    return {
        "http_status": status,
        "body_text": text,
        "body_json": parsed,
        "body_sha256": hashlib.sha256(raw_body).hexdigest(),
        "request_id": (
            _response_header(headers, "x-request-id")
            or _response_header(headers, "x-cmd-request-id")
        ),
    }


def normalize_probe_http_result(
    raw: Mapping[str, Any],
    *,
    expected_model: str,
) -> dict[str, Any]:
    """Normalize without raising on a definitive malformed/error HTTP body."""
    status = raw.get("http_status")
    body = raw.get("body_json")
    errors: list[str] = []

    if not isinstance(status, int) or isinstance(status, bool):
        errors.append("missing_or_invalid_http_status")
        status = None

    response_id = None
    response_model = None
    finish_reason = None
    content = None
    usage: dict[str, Any] | None = None
    error: dict[str, Any] | None = None

    if isinstance(body, Mapping):
        raw_id = body.get("id")
        if isinstance(raw_id, str):
            response_id = raw_id

        raw_model = body.get("model")
        if isinstance(raw_model, str):
            response_model = raw_model

        choices = body.get("choices")
        if isinstance(choices, list) and len(choices) == 1:
            choice = choices[0]
            if isinstance(choice, Mapping):
                raw_finish = choice.get("finish_reason")
                if raw_finish is None or isinstance(raw_finish, str):
                    finish_reason = raw_finish
                else:
                    errors.append("invalid_finish_reason")

                message = choice.get("message")
                if isinstance(message, Mapping):
                    raw_content = message.get("content")
                    if raw_content is None:
                        content = ""
                    elif isinstance(raw_content, str):
                        content = raw_content
                    else:
                        errors.append("invalid_content")
                else:
                    errors.append("missing_message")
            else:
                errors.append("invalid_choice")
        elif status == 200:
            errors.append("expected_exactly_one_choice")

        raw_usage = body.get("usage")
        if isinstance(raw_usage, Mapping):
            usage = {
                "prompt_tokens": raw_usage.get("prompt_tokens"),
                "completion_tokens": raw_usage.get("completion_tokens"),
                "total_tokens": raw_usage.get("total_tokens"),
            }
            details = raw_usage.get("prompt_tokens_details")
            if isinstance(details, Mapping):
                usage["cached_tokens"] = details.get("cached_tokens")

        raw_error = body.get("error")
        if isinstance(raw_error, Mapping):
            error = {
                "type": raw_error.get("type"),
                "code": raw_error.get("code"),
                "param": raw_error.get("param"),
                "message": (
                    str(raw_error.get("message"))[:1000]
                    if raw_error.get("message") is not None
                    else None
                ),
            }
    else:
        errors.append("response_body_not_json")

    return {
        "http_status": status,
        "expected_model": expected_model,
        "provider_response_id": response_id,
        "response_model": response_model,
        "finish_reason": finish_reason,
        "content": content,
        "usage": usage,
        "error": error,
        "normalization_errors": errors,
        "body_sha256": raw.get("body_sha256"),
        "request_id": raw.get("request_id"),
    }


def evaluate_probe_response(
    response: Mapping[str, Any],
    *,
    runtime_case: ProbeCaseRuntime,
) -> dict[str, Any]:
    """Fail-closed capability acceptance for one durable response envelope."""
    reasons: list[str] = []

    if response.get("http_status") != 200:
        reasons.append(f"http_status_{response.get('http_status')}")

    expected_model = runtime_case.case.provider_model_id
    if response.get("response_model") != expected_model:
        reasons.append("provider_model_identity_mismatch")

    normalization_errors = response.get("normalization_errors")
    if isinstance(normalization_errors, list) and normalization_errors:
        reasons.extend(
            f"normalization:{item}"
            for item in normalization_errors
            if isinstance(item, str)
        )

    usage = response.get("usage")
    if not isinstance(usage, Mapping):
        reasons.append("missing_usage")
    else:
        for name in ("prompt_tokens", "completion_tokens"):
            value = usage.get(name)
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < 0
            ):
                reasons.append(f"missing_or_invalid_{name}")

    return {
        "accepted": not reasons,
        "reasons": reasons,
        "benchmark_model_id": runtime_case.case.benchmark_model_id,
        "provider_model_id": runtime_case.case.provider_model_id,
        "effort": runtime_case.case.effort,
        "reasoning_field_sent": (
            REASONING_WIRE_FIELD if runtime_case.case.effort is not None else None
        ),
        "zdr_enabled": False,
    }


def _write_plan_if_absent(root: Path, plan: Mapping[str, Any]) -> Path:
    path = root / "probe_plan.json"
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if _sha256_json(existing) != _sha256_json(plan):
            raise CapabilityProbePlanError(
                "existing G5C probe_plan.json does not match the frozen plan"
            )
        return path
    _atomic_write_json(path, plan)
    return path


def inspect_probe_state(evidence_root: Path | str) -> dict[str, Any]:
    """Read existing journals and compute the exact number of new sends needed."""
    root = Path(evidence_root)
    cases = build_probe_runtime_cases(root)
    records = []
    new_calls_required = 0

    for item in cases:
        record = load_matching_journal(item.journal_path, item.identity)
        status = None if record is None else record["status"]
        if status in {"started", "uncertain"}:
            raise PaidRequestUncertainError(
                "G5C contains an ambiguous paid request; automatic continuation "
                f"is blocked: {item.journal_path}"
            )
        if status != "response_recorded":
            new_calls_required += 1
        records.append(
            {
                "index": item.index,
                "benchmark_model_id": item.case.benchmark_model_id,
                "provider_model_id": item.case.provider_model_id,
                "effort": item.case.effort,
                "journal_status": status or "absent",
            }
        )

    return {
        "case_count": len(cases),
        "new_calls_required": new_calls_required,
        "records": records,
    }


def run_probe_suite(
    *,
    evidence_root: Path | str,
    api_key: str,
    authorize_new_calls: int,
    sender: Callable[..., Mapping[str, Any]] = send_probe_http_once,
) -> dict[str, Any]:
    """Execute/replay the frozen G5C suite sequentially under durable journals."""
    root = Path(evidence_root)
    state = inspect_probe_state(root)

    if (
        not isinstance(authorize_new_calls, int)
        or isinstance(authorize_new_calls, bool)
        or authorize_new_calls < 0
    ):
        raise CapabilityProbeAuthorizationError(
            "authorized new-call count must be a non-negative integer"
        )
    if authorize_new_calls != state["new_calls_required"]:
        raise CapabilityProbeAuthorizationError(
            "authorized new-call count does not equal the exact remaining G5C "
            f"send count: authorized={authorize_new_calls}, "
            f"required={state['new_calls_required']}"
        )
    if authorize_new_calls and (not isinstance(api_key, str) or not api_key.strip()):
        raise CapabilityProbeAuthorizationError(
            f"{PROBE_API_KEY_ENV} is required before any new provider send"
        )

    root.mkdir(parents=True, exist_ok=True)
    plan = probe_plan_identity(root)
    plan_path = _write_plan_if_absent(root, plan)

    runtime_cases = build_probe_runtime_cases(root)
    result_dir = root / "results"
    result_dir.mkdir(parents=True, exist_ok=True)

    api_calls_this_run = 0
    results = []

    for item in runtime_cases:
        def prepare_attempt(item=item) -> Mapping[str, Any]:
            return {
                "probe_revision": G5C_PROBE_REVISION,
                "case_index": item.index,
                "benchmark_model_id": item.case.benchmark_model_id,
                "provider_model_id": item.case.provider_model_id,
                "effort": item.case.effort,
                "reasoning_wire_field": (
                    REASONING_WIRE_FIELD
                    if item.case.effort is not None
                    else None
                ),
                "zdr_enabled": False,
                "provider_url": PROVIDER_CHAT_URL,
            }

        def send_once(item=item) -> Mapping[str, Any]:
            return sender(
                api_key=api_key,
                request_body=item.request_body,
                url=PROVIDER_CHAT_URL,
                timeout_seconds=PROBE_TIMEOUT_SECONDS,
            )

        def normalize(raw, _attempt, item=item) -> Mapping[str, Any]:
            if not isinstance(raw, Mapping):
                return {
                    "http_status": None,
                    "expected_model": item.case.provider_model_id,
                    "provider_response_id": None,
                    "response_model": None,
                    "finish_reason": None,
                    "content": None,
                    "usage": None,
                    "error": None,
                    "normalization_errors": ["raw_http_result_not_mapping"],
                    "body_sha256": None,
                    "request_id": None,
                }
            return normalize_probe_http_result(
                raw,
                expected_model=item.case.provider_model_id,
            )

        journal_result = run_journaled_paid_request(
            journal_path=item.journal_path,
            identity=item.identity,
            save_json=_atomic_write_json,
            prepare_attempt=prepare_attempt,
            send_once=send_once,
            normalize_response=normalize,
        )

        if journal_result["api_called"]:
            api_calls_this_run += 1

        response = journal_result["response"]
        evaluation = evaluate_probe_response(
            response,
            runtime_case=item,
        )
        result_record = {
            "schema_version": PROBE_SCHEMA_VERSION,
            "probe_revision": G5C_PROBE_REVISION,
            "case": item.plan_identity(),
            "api_called_this_run": journal_result["api_called"],
            "replayed": journal_result["replayed"],
            "response": response,
            "evaluation": evaluation,
        }
        result_path = result_dir / f"{item.index:04d}.json"
        _atomic_write_json(result_path, result_record)
        results.append(result_record)

        if not evaluation["accepted"]:
            summary = {
                "schema_version": PROBE_SCHEMA_VERSION,
                "probe_revision": G5C_PROBE_REVISION,
                "plan_sha256": _sha256_json(plan),
                "status": "stopped_on_definitive_rejection",
                "api_calls_this_run": api_calls_this_run,
                "completed_case_count": len(results),
                "failed_case_index": item.index,
                "results": [
                    {
                        "index": record["case"]["index"],
                        "evaluation": record["evaluation"],
                    }
                    for record in results
                ],
            }
            _atomic_write_json(root / "probe_summary.json", summary)
            raise CapabilityProbeRejectedError(
                "G5C stopped after a durable definitive capability rejection at "
                f"case {item.index}: {evaluation['reasons']}"
            )

    summary = {
        "schema_version": PROBE_SCHEMA_VERSION,
        "probe_revision": G5C_PROBE_REVISION,
        "plan_sha256": _sha256_json(plan),
        "status": "accepted_complete",
        "case_count": len(results),
        "accepted_count": sum(
            1 for record in results if record["evaluation"]["accepted"]
        ),
        "api_calls_this_run": api_calls_this_run,
        "replayed_count": sum(1 for record in results if record["replayed"]),
        "provider_calls_total_bound": len(results),
        "capability_freeze_sha256": CAPABILITY_FREEZE_SHA256,
        "results": [
            {
                "index": record["case"]["index"],
                "benchmark_model_id": record["evaluation"]["benchmark_model_id"],
                "provider_model_id": record["evaluation"]["provider_model_id"],
                "effort": record["evaluation"]["effort"],
                "accepted": record["evaluation"]["accepted"],
            }
            for record in results
        ],
    }
    _atomic_write_json(root / "probe_summary.json", summary)

    manifest = {
        "schema_version": PROBE_SCHEMA_VERSION,
        "execution_revision": PROBE_EXECUTION_REVISION,
        "probe_revision": G5C_PROBE_REVISION,
        "capability_freeze_sha256": CAPABILITY_FREEZE_SHA256,
        "plan_sha256": _sha256_json(plan),
        "plan_file": plan_path.name,
        "summary_sha256": hashlib.sha256(
            (root / "probe_summary.json").read_bytes()
        ).hexdigest(),
        "status": "accepted_complete",
        "new_provider_calls_this_run": api_calls_this_run,
        "credentials_persisted": False,
        "zdr_enabled": False,
    }
    _atomic_write_json(root / "probe_manifest.json", manifest)
    return summary


def read_probe_api_key() -> str:
    """Read the process-only G5C API key at explicit execution time."""
    return os.environ.get(PROBE_API_KEY_ENV, "")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run/replay the journal-protected GOAT G5C capability probes."
    )
    parser.add_argument(
        "--evidence-root",
        required=True,
        help="Experiment-owned G5C evidence directory; never a novel directory.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Authorize runtime execution path. Without this flag no credential is read.",
    )
    parser.add_argument(
        "--authorize-new-calls",
        type=int,
        default=0,
        help="Exact number of new provider sends authorized for this invocation.",
    )
    parser.add_argument(
        "--confirm-probe-revision",
        default="",
        help=f"Must equal {G5C_PROBE_REVISION!r} when --execute is used.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    state = inspect_probe_state(args.evidence_root)
    print(f"probe_revision={G5C_PROBE_REVISION}")
    print(f"case_count={state['case_count']}")
    print(f"new_calls_required={state['new_calls_required']}")
    print(f"zdr_enabled={BENCHMARK_ZDR_ENABLED}")

    if not args.execute:
        print("execution=disabled")
        return 0

    if args.confirm_probe_revision != G5C_PROBE_REVISION:
        raise CapabilityProbeAuthorizationError(
            "--confirm-probe-revision must exactly match the frozen G5C revision"
        )

    api_key = read_probe_api_key()
    summary = run_probe_suite(
        evidence_root=args.evidence_root,
        api_key=api_key,
        authorize_new_calls=args.authorize_new_calls,
    )
    print(f"status={summary['status']}")
    print(f"api_calls_this_run={summary['api_calls_this_run']}")
    print(f"accepted_count={summary['accepted_count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
