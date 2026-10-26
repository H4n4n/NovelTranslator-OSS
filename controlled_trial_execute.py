"""Fail-closed executor for one explicitly approved paid production trial.

Nothing in this module authorizes spending by itself. The CLI rebuilds the
read-only controlled-trial plan, requires its exact state fingerprint and
approval token, and still performs zero provider calls unless the caller also
supplies ``--authorize-paid-trial``.

With explicit authorization, the executor loads the real import-safe production
module through the shared callable boundary. A hard provider-budget proxy is
installed before runtime initialization, then the production runner is invoked
with the exact approved target chapter. The legacy module's ``main()`` is never
dispatched and no transformed copy of the production source is executed.
"""

from production_provider_contract import PROVIDER_COMMAND_CODE
import argparse
import hashlib
import json
import os
import sys
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from threading import Lock
from datetime import datetime, timezone
from pathlib import Path

import noveltranslator_core as core
from controlled_trial_plan import build_controlled_trial_plan
from novel_project import load_novel_project, repository_root
from production_failure_classification import NonProviderProductionError
from production_operation_policy import ProductionOperationPolicy, policy_for_model
from production_run_lock import production_run_lock
from production_output_runtime import model_aware_legacy_print
from run_production_engine import (
    PRODUCTION_MODULE_NAME,
    PRODUCTION_SCRIPT,
    load_production_module,
    prepare_production_runtime,
)
from translation_model_config import resolve_translation_model


EXECUTION_MODE = "controlled_one_chapter_paid_trial"
PROMPT_TOKEN_SAFETY_OVERHEAD = 8192


class ControlledTrialExecutionError(RuntimeError):
    pass


class ControlledTrialBudgetError(ControlledTrialExecutionError):
    pass


class ControlledTrialPreSendRefusal(ControlledTrialBudgetError, NonProviderProductionError):
    """Deterministic local refusal; no physical provider attempt occurred."""
    failure_kind = "control"
    deterministic_local_budget_refusal = True


@dataclass(frozen=True)
class _RequestAuthorization:
    request_json: str
    pricing_json: str
    conservative_cost_bound_usd: float


def _usage_value(usage, name):
    value = getattr(usage, name, 0) if usage is not None else 0
    return int(value or 0)


def _pricing_info(config, moment):
    period = "peak" if core.is_deepseek_peak_time(moment) else "off_peak"
    rates = config.prices[period]
    return {
        "pricing_version": config.pricing_version,
        "pricing_period": period,
        "request_started_utc": moment.isoformat(),
        "cache_hit_rate_per_million": rates["cache_hit"],
        "cache_miss_rate_per_million": rates["cache_miss"],
        "output_rate_per_million": rates["output"],
    }


def _serialized_request_bytes(kwargs):
    return len(json.dumps(
        kwargs,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=repr,
    ).encode("utf-8"))


class ControlledTrialBudget:
    """Count actual provider attempts and conservatively gate future cost."""

    def __init__(self, config, max_api_calls, max_cost_usd, *, operation_policy=None):
        if not isinstance(max_api_calls, int) or isinstance(max_api_calls, bool) or max_api_calls < 1:
            raise ControlledTrialBudgetError("max_api_calls must be a positive integer")
        if isinstance(max_cost_usd, bool) or not isinstance(max_cost_usd, (int, float)):
            raise ControlledTrialBudgetError("max_cost_usd must be a positive number")
        if float(max_cost_usd) <= 0:
            raise ControlledTrialBudgetError("max_cost_usd must be greater than zero")
        self.config = resolve_translation_model(
            config.model if hasattr(config, "model") else config
        )
        canonical_policy = policy_for_model(self.config.model)
        if operation_policy is None:
            operation_policy = canonical_policy
        if (not isinstance(operation_policy, ProductionOperationPolicy)
            or operation_policy != canonical_policy):
            raise ControlledTrialBudgetError(
                "trial operation policy does not match the selected production model policy"
            )
        self.operation_policy = operation_policy
        self.max_api_calls = max_api_calls
        self.max_cost_usd = float(max_cost_usd)
        self.api_calls = 0
        self.actual_cost_usd = 0.0
        self.records = []
        self.pre_send_refusals = []
        self._authorization_lock = Lock()
        self._active_authorization = None
        self._authorization_used = False

    def _worst_case_cost(self, kwargs, pricing):
        # Deliberately conservative. UTF-8 request bytes plus fixed chat/tool
        # overhead are treated as an upper-bound token proxy; all input is then
        # priced as cache-miss input. Exact usage is accounted after success.
        prompt_upper = _serialized_request_bytes(kwargs) + PROMPT_TOKEN_SAFETY_OVERHEAD
        output_upper = kwargs.get("max_tokens")
        if type(output_upper) is not int or output_upper < 1:
            raise ControlledTrialPreSendRefusal("provider max_tokens must be an explicit positive integer")
        controls = {"max_tokens": output_upper, "extra_body": kwargs.get("extra_body")}
        if "reasoning_effort" in kwargs:
            controls["reasoning_effort"] = kwargs["reasoning_effort"]
        # Exact containers also reject controls hidden in extra_body, including
        # an unexpected reasoning_effort or a provider ceiling override.
        if ("max_completion_tokens" in kwargs or "thinking" in kwargs
            or not any(controls == operation.request_controls()
                       for operation in self.operation_policy.operations())):
            raise ControlledTrialPreSendRefusal(
                "provider request controls do not match an authorized production operation policy"
            )
        return (
            prompt_upper / 1_000_000 * pricing["cache_miss_rate_per_million"]
            + output_upper / 1_000_000 * pricing["output_rate_per_million"]
        )

    def _authorize_request(self, kwargs):
        """Only local checks, before the possible transport boundary."""
        try:
            request_json = json.dumps(kwargs, ensure_ascii=False, sort_keys=True,
                                      separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ControlledTrialPreSendRefusal("provider request is not canonical JSON") from exc
        kwargs = json.loads(request_json)
        if not self.config.new_requests_allowed:
            raise ControlledTrialPreSendRefusal("new provider requests cannot use a retired model")
        request_model = kwargs.get("model")
        if request_model != self.config.model:
            raise ControlledTrialPreSendRefusal(
                f"provider request model {request_model!r} does not match approved "
                f"model {self.config.model!r}"
            )
        if self.api_calls >= self.max_api_calls:
            raise ControlledTrialPreSendRefusal(
                f"controlled trial API-call ceiling reached ({self.max_api_calls})"
            )

        started = datetime.now(timezone.utc)
        pricing = _pricing_info(self.config, started)
        worst_case = self._worst_case_cost(kwargs, pricing)
        remaining = self.max_cost_usd - self.actual_cost_usd
        if worst_case > remaining + 1e-12:
            raise ControlledTrialPreSendRefusal(
                "controlled trial cost ceiling would be unsafe for the next request: "
                f"remaining=${remaining:.6f}, conservative request bound=${worst_case:.6f}"
            )

        return _RequestAuthorization(request_json, json.dumps(pricing, sort_keys=True), worst_case)

    @contextmanager
    def authorize_request(self, **kwargs):
        """Lease one immutable request until send or local persistence failure.

        Holding the lease prevents a different call from consuming capacity
        between authorization and transport. This is not a parallel executor.
        """
        if not self._authorization_lock.acquire(blocking=False):
            raise ControlledTrialPreSendRefusal("another request authorization is active")
        try:
            try:
                authorization = self._authorize_request(kwargs)
            except ControlledTrialPreSendRefusal as exc:
                self.pre_send_refusals.append({"kind": "deterministic_local_budget_refusal", "reason": str(exc)})
                raise
            self._active_authorization = authorization
            self._authorization_used = False
            yield authorization
        finally:
            self._active_authorization = None
            self._authorization_used = False
            self._authorization_lock.release()

    def create(self, client, **kwargs):
        """Unjournaled compatibility path: authorize locally, then send once."""
        with self.authorize_request(**kwargs) as authorization:
            return self.send_authorized(client, authorization)

    def send_authorized(self, client, authorization):
        """Consume the active lease once; never repeat mutable budget checks."""
        if (not isinstance(authorization, _RequestAuthorization)
            or authorization is not self._active_authorization or self._authorization_used):
            raise ControlledTrialBudgetError("request authorization is inactive or already consumed")
        self._authorization_used = True
        kwargs = json.loads(authorization.request_json)
        pricing = json.loads(authorization.pricing_json)
        request_model = kwargs["model"]
        worst_case = authorization.conservative_cost_bound_usd
        record = {
            "call": self.api_calls + 1,
            "model": request_model,
            "pricing_period": pricing["pricing_period"],
            "pricing_version": pricing["pricing_version"],
            "conservative_cost_bound_usd": worst_case,
            "status": "started",
            "actual_cost_usd": 0.0,
        }
        self.records.append(record)
        try:
            self.api_calls += 1
            response = client.chat.completions.create(**kwargs)
        except Exception:
            record["status"] = "provider_error"
            raise

        usage = getattr(response, "usage", None)
        if usage is None:
            # A provider response may already have incurred cost. Mark this
            # explicitly rather than pretending the trial spent zero.
            record["status"] = "missing_usage_unaccounted_response"
            raise ControlledTrialBudgetError(
                "provider returned no usage accounting; refusing any further paid work"
            )

        prompt_tokens = _usage_value(usage, "prompt_tokens")
        completion_tokens = _usage_value(usage, "completion_tokens")
        cache_hits = _usage_value(usage, "prompt_cache_hit_tokens")
        cache_misses = _usage_value(usage, "prompt_cache_miss_tokens")
        actual_cost, normalized_hits, normalized_misses = (
            core.calculate_cost_from_token_counts(
                prompt_tokens,
                completion_tokens,
                cache_hits,
                cache_misses,
                pricing,
            )
        )
        projected = self.actual_cost_usd + actual_cost
        record.update({
            "status": "success",
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "cache_hit_tokens": normalized_hits,
            "cache_miss_tokens": normalized_misses,
            "actual_cost_usd": actual_cost,
        })
        self.actual_cost_usd = projected
        if projected > self.max_cost_usd + 1e-12:
            record["status"] = "cost_ceiling_exceeded_after_response"
            raise ControlledTrialBudgetError(
                "provider usage exceeded the controlled trial cost ceiling; refusing further paid work"
            )
        return response



_CONTROLLED_TRIAL_ALLOW_PEAK = False


class _BudgetedCompletionsProxy:
    def __init__(self, real_client, budget):
        self._real_client = real_client
        self._real_completions = real_client.chat.completions
        self._budget = budget
        self._authorization = None

    @contextmanager
    def authorize_journaled_send(self, *, send_once, **kwargs):
        with self._budget.authorize_request(**kwargs) as authorization:
            def authorized_send():
                # Arm the proxy only when the journal invokes transport after
                # STARTED; entering authorization alone cannot dispatch it.
                self._authorization = authorization
                try:
                    return send_once()
                finally:
                    self._authorization = None
            yield authorized_send

    def create(self, **kwargs):
        if self._authorization is not None:
            return self._budget.send_authorized(self._real_client, self._authorization)
        return self._budget.create(self._real_client, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real_completions, name)


class _BudgetedChatProxy:
    def __init__(self, real_client, budget):
        self._real_chat = real_client.chat
        self.completions = _BudgetedCompletionsProxy(
            real_client,
            budget,
        )

    def __getattr__(self, name):
        return getattr(self._real_chat, name)


class _BudgetedClientProxy:
    def __init__(self, real_client, budget):
        self._real_client = real_client
        self.chat = _BudgetedChatProxy(real_client, budget)

    def __getattr__(self, name):
        return getattr(self._real_client, name)


def _budgeted_openai_factory(real_factory, budget):
    """Wrap every production OpenAI client in the hard trial budget."""

    def create_client(*args, **kwargs):
        real_client = real_factory(*args, **kwargs)
        return _BudgetedClientProxy(real_client, budget)

    return create_client

def _hash_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _snapshot_mutable_tree(project):
    roots = (project.data_dir, project.output_root, project.backup_dir, project.logs_dir)
    files = {}
    for root in roots:
        root = Path(root)
        if not root.exists():
            continue
        for item in sorted(p for p in root.rglob("*") if p.is_file()):
            files[item.relative_to(project.root).as_posix()] = _hash_file(item)
    return files


def _changed_paths(before, after):
    return sorted(
        path for path in set(before) | set(after) if before.get(path) != after.get(path)
    )


def _read_progress(project):
    with project.progress_file.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ControlledTrialExecutionError("progress.json is not an object after trial")
    return value


def _numeric_files(path, suffix):
    path = Path(path)
    if not path.is_dir():
        return set()
    return {
        int(item.stem)
        for item in path.glob(f"*{suffix}")
        if item.is_file() and item.stem.isdigit()
    }


def _capture_core_runtime():
    names = (
        "PRODUCTION_TRANSLATION_MODEL",
        "PRODUCTION_TRANSLATION_MAX_TOKENS",
        "PRODUCTION_TRANSLATION_THINKING",
        "V4_PRO_PRICES",
        "PRICING_VERSION",
        "atomic_write_text",
        "atomic_write_json",
        "translation_commit_qa",
    )
    state = {name: getattr(core, name) for name in names}
    state["semantic_present"] = hasattr(core, "_semantic_shadow_runtime_state")
    state["semantic_state"] = getattr(core, "_semantic_shadow_runtime_state", None)
    return state


def _restore_core_runtime(state):
    for name, value in state.items():
        if name in {"semantic_present", "semantic_state"}:
            continue
        setattr(core, name, value)
    if state["semantic_present"]:
        core._semantic_shadow_runtime_state = state["semantic_state"]
    elif hasattr(core, "_semantic_shadow_runtime_state"):
        delattr(core, "_semantic_shadow_runtime_state")


@contextmanager
def _working_directory(path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def validate_execution_authorization(
    novel_id,
    *,
    repo_root,
    model_selection,
    max_api_calls,
    max_cost_usd,
    expected_state_fingerprint,
    approval_token,
    provider_selection=None,
    max_credit_equivalent_usd=None,
):
    plan = build_controlled_trial_plan(
        novel_id,
        repo_root=repo_root,
        model_selection=model_selection,
        max_api_calls=max_api_calls,
        max_cost_usd=max_cost_usd,
        **({"provider_selection": provider_selection} if provider_selection is not None else {}),
        **({"max_credit_equivalent_usd": max_credit_equivalent_usd} if max_credit_equivalent_usd is not None else {}),
        allow_peak=_CONTROLLED_TRIAL_ALLOW_PEAK,
    )
    if plan["state_fingerprint"] != expected_state_fingerprint:
        raise ControlledTrialExecutionError(
            "state fingerprint does not match the current project; regenerate the read-only plan"
        )
    if plan["approval_token"] != approval_token:
        raise ControlledTrialExecutionError(
            "approval token does not match the current controlled-trial plan"
        )
    return plan


def _post_run_report(project, plan, config, budget, before, run_error=None):
    after = _snapshot_mutable_tree(project)
    try:
        progress = _read_progress(project)
    except Exception as exc:
        progress = {}
        run_error = run_error or f"cannot read post-trial progress: {exc}"
    committed = _numeric_files(project.chapter_dir, ".md")
    pending = _numeric_files(project.pending_dir, ".md") | _numeric_files(
        project.pending_dir, ".json"
    )
    unattended = plan.get("mode") == "bounded_goat_unattended"
    target = plan["final_chapter"] if unattended else plan["target_chapter"]
    raw_completed = progress.get("completed", [])
    completed = raw_completed if isinstance(raw_completed, list) else []
    numeric_completed = {
        value for value in completed
        if isinstance(value, int) and not isinstance(value, bool)
    }
    escaped = sorted(number for number in committed | pending | numeric_completed if number > target)
    committed_ok = (
        target in committed
        and target in numeric_completed
        and progress.get("last_completed") == target
        and progress.get("next_chapter") == target + 1
    )
    if escaped:
        status = "BOUNDARY_VIOLATION"
        run_error = (
            run_error or "controlled trial escaped its one-chapter boundary: "
            + ", ".join(map(str, escaped))
        )
    elif (unattended and budget.pre_send_refusals and not budget.blocked
          and all(item["kind"] == "deterministic_local_budget_refusal" for item in budget.pre_send_refusals)):
        status = "ALLOWANCE_EXHAUSTED"
    elif run_error:
        status = "FAILED_AFTER_AUTHORIZATION"
    elif committed_ok:
        status = "COMMITTED"
    else:
        status = "INCOMPLETE"

    unaccounted = any(
        record.get("status") == "missing_usage_unaccounted_response"
        for record in budget.records
    )
    return {
        "mode": "bounded_goat_unattended" if unattended else EXECUTION_MODE,
        "last_completed": progress.get("last_completed"),
        "next_chapter": progress.get("next_chapter"),
        "status": status,
        "paid_execution_authorized": True,
        "novel_id": plan["novel_id"],
        "target_chapter": target,
        "model": config.model,
        "model_version": config.version,
        "pricing_version": config.pricing_version,
        "scheduler_mode": plan["scheduler_mode"],
        "allow_peak": plan["allow_peak"],
        "max_api_calls": budget.max_api_calls,
        "api_calls": budget.api_calls,
        "max_cost_usd": budget.max_cost_usd,
        "actual_cost_usd": budget.actual_cost_usd,
        "unaccounted_provider_response": unaccounted,
        "provider_calls": budget.records,
        "pre_send_refusals": [dict(item) for item in budget.pre_send_refusals],
        "target_committed": committed_ok,
        "target_pending_preserved": target in pending,
        "pending_chapters": sorted(pending),
        "escaped_chapters": escaped,
        "changed_paths": _changed_paths(before, after),
        "state_fingerprint": plan["state_fingerprint"],
        "approval_token": plan["approval_token"],
        "error": run_error or "",
    }


def execute_controlled_trial(
    novel_id,
    *,
    repo_root,
    model_selection,
    max_api_calls,
    max_cost_usd,
    expected_state_fingerprint,
    approval_token,
    provider_selection=None,
    max_credit_equivalent_usd=None,
    authorize_paid_trial=False,
):
    """Revalidate and execute while holding the novel lock only for paid opt-in."""
    lock = (
        production_run_lock(load_novel_project(novel_id, repo_root=repo_root).root)
        if authorize_paid_trial else nullcontext()
    )
    with lock:
        return _execute_controlled_trial_under_lock(
            novel_id,
            repo_root=repo_root,
            model_selection=model_selection,
            max_api_calls=max_api_calls,
            max_cost_usd=max_cost_usd,
            **({"provider_selection": provider_selection} if provider_selection is not None else {}),
            **({"max_credit_equivalent_usd": max_credit_equivalent_usd} if max_credit_equivalent_usd is not None else {}),
            expected_state_fingerprint=expected_state_fingerprint,
            approval_token=approval_token,
            authorize_paid_trial=authorize_paid_trial,
        )


def _execute_controlled_trial_under_lock(
    novel_id,
    *,
    repo_root,
    model_selection,
    max_api_calls,
    max_cost_usd,
    expected_state_fingerprint,
    approval_token,
    provider_selection=None,
    max_credit_equivalent_usd=None,
    authorize_paid_trial=False,
):
    """Validate the exact plan and, only with explicit opt-in, run one chapter."""
    plan = validate_execution_authorization(
        novel_id,
        repo_root=repo_root,
        model_selection=model_selection,
        max_api_calls=max_api_calls,
        max_cost_usd=max_cost_usd,
        **({"provider_selection": provider_selection} if provider_selection is not None else {}),
        **({"max_credit_equivalent_usd": max_credit_equivalent_usd} if max_credit_equivalent_usd is not None else {}),
        expected_state_fingerprint=expected_state_fingerprint,
        approval_token=approval_token,
    )
    if not authorize_paid_trial:
        return {
            "mode": EXECUTION_MODE,
            "status": "AUTHORIZATION_REQUIRED",
            "paid_execution_authorized": False,
            **({key: plan[key] for key in ("provider", "provider_identity", "budget_unit",
                "max_credit_equivalent_usd")} if "provider" in plan else {}),
            "api_calls": 0,
            "actual_cost_usd": 0.0,
            "novel_id": plan["novel_id"],
            "target_chapter": plan["target_chapter"],
            "model": plan["requested_model"],
            "scheduler_mode": plan["scheduler_mode"],
            "allow_peak": plan["allow_peak"],
            "state_fingerprint": plan["state_fingerprint"],
            "approval_token": plan["approval_token"],
            "changed_paths": [],
            "error": "",
        }

    if plan.get("provider") == PROVIDER_COMMAND_CODE:
        from production_goat_execution import execute_authorized_goat
        return execute_authorized_goat(plan, repo_root=repo_root, recovery=False)

    project = load_novel_project(novel_id, repo_root=repo_root)
    config = resolve_translation_model(model_selection)
    before = _snapshot_mutable_tree(project)
    core_state = _capture_core_runtime()
    budget = ControlledTrialBudget(
        config, max_api_calls, max_cost_usd,
        operation_policy=policy_for_model(config.model),
    )
    run_error = None

    missing_production_module = object()
    previous_production_module = sys.modules.get(
        PRODUCTION_MODULE_NAME,
        missing_production_module,
    )

    try:
        with _working_directory(project.root):
            prepared = prepare_production_runtime(
                config.model,
                project.pending_dir,
                core_module=core,
                reporter=None,
            )
            if prepared["runtime"]["effective_model"] != config.model:
                raise ControlledTrialExecutionError(
                    "effective model changed after authorization"
                )

            with model_aware_legacy_print(config):
                production_module = load_production_module(
                    PRODUCTION_SCRIPT
                )

                initializer = getattr(
                    production_module,
                    "initialize_production_runtime_state",
                    None,
                )
                bounded_runner = getattr(
                    production_module,
                    "run_unattended_production",
                    None,
                )
                real_openai_factory = getattr(
                    production_module,
                    "OpenAI",
                    None,
                )

                if not callable(initializer):
                    raise ControlledTrialExecutionError(
                        "production runtime initializer was not defined"
                    )
                if not callable(bounded_runner):
                    raise ControlledTrialExecutionError(
                        "production unattended runner was not defined"
                    )
                if not callable(real_openai_factory):
                    raise ControlledTrialExecutionError(
                        "production OpenAI client factory was not defined"
                    )

                production_module.OpenAI = _budgeted_openai_factory(
                    real_openai_factory,
                    budget,
                )

                if (
                    plan["allow_peak"]
                    is not _CONTROLLED_TRIAL_ALLOW_PEAK
                ):
                    raise ControlledTrialExecutionError(
                        "controlled-trial scheduler mode changed "
                        "after authorization"
                    )

                scheduler_value = getattr(
                    production_module,
                    "AUTO_OFF_PEAK_ONLY",
                    None,
                )

                if not isinstance(scheduler_value, bool):
                    raise ControlledTrialExecutionError(
                        "production AUTO_OFF_PEAK_ONLY scheduler "
                        "setting was not defined"
                    )

                expected_scheduler_value = (
                    not plan["allow_peak"]
                )

                if plan["allow_peak"]:
                    production_module.AUTO_OFF_PEAK_ONLY = False
                elif scheduler_value is not True:
                    raise ControlledTrialExecutionError(
                        "off-peak-only controlled trial requires "
                        "AUTO_OFF_PEAK_ONLY = True"
                    )

                initializer()

                if (
                    getattr(
                        production_module,
                        "AUTO_OFF_PEAK_ONLY",
                        None,
                    )
                    is not expected_scheduler_value
                ):
                    raise ControlledTrialExecutionError(
                        "production scheduler mode changed during "
                        "initialization"
                    )

                runtime_client = getattr(
                    production_module,
                    "client",
                    None,
                )
                if not isinstance(
                    runtime_client,
                    _BudgetedClientProxy,
                ):
                    raise ControlledTrialExecutionError(
                        "production client escaped controlled-trial "
                        "provider proxy"
                    )

                if (
                    getattr(
                        production_module,
                        "run_unattended_production",
                        None,
                    )
                    is not bounded_runner
                ):
                    raise ControlledTrialExecutionError(
                        "production unattended runner changed during "
                        "initialization"
                    )

                runtime_progress = getattr(
                    production_module,
                    "progress",
                    None,
                )
                if not isinstance(runtime_progress, dict):
                    raise ControlledTrialExecutionError(
                        "production progress state was not initialized"
                    )
                if (
                    runtime_progress.get("next_chapter")
                    != plan["target_chapter"]
                ):
                    raise ControlledTrialExecutionError(
                        "production next chapter changed before "
                        "bounded trial invocation"
                    )

                bounded_runner(
                    target_chapter=plan["target_chapter"]
                )

    except SystemExit as exc:
        if exc.code not in (None, 0):
            run_error = run_error or (
                "legacy production runner exited with "
                f"status {exc.code}"
            )
    except KeyboardInterrupt:
        run_error = run_error or (
            "operator interrupted the controlled trial"
        )
    except Exception as exc:
        run_error = run_error or (
            f"{type(exc).__name__}: {exc}"
        )
    finally:
        _restore_core_runtime(core_state)

        if previous_production_module is missing_production_module:
            sys.modules.pop(
                PRODUCTION_MODULE_NAME,
                None,
            )
        else:
            sys.modules[
                PRODUCTION_MODULE_NAME
            ] = previous_production_module

    return _post_run_report(
        project,
        plan,
        config,
        budget,
        before,
        run_error,
    )


def _print_human(report):
    if report.get("provider") == PROVIDER_COMMAND_CODE:
        print("Provider: command_code_provider; budget/accounting: credit-equivalent USD (no invoice claim)")
        print(json.dumps(report["provider_identity"], ensure_ascii=False, sort_keys=True))
    print("=" * 70)
    print("CONTROLLED ONE-CHAPTER PRODUCTION TRIAL EXECUTOR")
    print("=" * 70)
    print(f"Status: {report['status']}")
    print(f"Novel: {report['novel_id']}")
    print(f"Target chapter: {report['target_chapter']}")
    print(f"Pinned model: {report['model']}")
    print("Paid execution authorized: " + (
        "YES" if report["paid_execution_authorized"] else "NO"
    ))
    print(f"API calls: {report['api_calls']}")
    if report.get("provider") == PROVIDER_COMMAND_CODE:
        print(f"Actual accounted usage: {report['actual_cost_usd']:.6f} credit-equivalent USD; provider_invoice_claimed=false")
    else:
        print(f"Actual accounted cost: ${report['actual_cost_usd']:.6f}")
    if report.get("paid_execution_authorized"):
        print("Target committed: " + (
            "YES" if report.get("target_committed") else "NO"
        ))
        print("Target pending preserved: " + (
            "YES" if report.get("target_pending_preserved") else "NO"
        ))
        print("Unaccounted provider response: " + (
            "YES" if report.get("unaccounted_provider_response") else "NO"
        ))
        print(f"Changed paths: {len(report.get('changed_paths', []))}")
        for path in report.get("changed_paths", []):
            print(f"  {path}")
        if report.get("error"):
            print(f"Error: {report['error']}")
    else:
        print("No provider request was made. Paid execution still requires explicit approval.")
    print("=" * 70)


def build_parser():
    parser = argparse.ArgumentParser(
        description="Execute exactly one state-bound production chapter under hard paid-call limits."
    )
    parser.add_argument("--provider", default=None)
    parser.add_argument("--max-credit-equivalent-usd", type=float, default=None)
    parser.add_argument("--novel", required=True)
    parser.add_argument("--repo-root", type=Path, default=repository_root())
    parser.add_argument("--model", required=True)
    parser.add_argument("--max-api-calls", type=int, required=True)
    parser.add_argument("--max-cost-usd", type=float, default=None)
    parser.add_argument("--state-fingerprint", required=True)
    parser.add_argument("--approval-token", required=True)
    parser.add_argument("--authorize-paid-trial", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        report = execute_controlled_trial(
            args.novel,
            repo_root=args.repo_root,
            model_selection=args.model,
            max_api_calls=args.max_api_calls,
            max_cost_usd=args.max_cost_usd,
            provider_selection=args.provider,
            max_credit_equivalent_usd=args.max_credit_equivalent_usd,
            expected_state_fingerprint=args.state_fingerprint,
            approval_token=args.approval_token,
            authorize_paid_trial=args.authorize_paid_trial,
        )
    except Exception as exc:
        # Authorization validation fails before the paid executor exists, so this
        # path can truthfully report zero calls. Once authorization is accepted,
        # execute_controlled_trial returns a failure report with real counters.
        report = {
            "mode": EXECUTION_MODE,
            "status": "REFUSED_BEFORE_EXECUTION",
            "paid_execution_authorized": False,
            "api_calls": 0,
            "actual_cost_usd": 0.0,
            "error": str(exc),
        }
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print("=" * 70)
            print("CONTROLLED ONE-CHAPTER PRODUCTION TRIAL EXECUTOR")
            print("=" * 70)
            print("Status: REFUSED_BEFORE_EXECUTION")
            print(f"Error: {exc}")
            print("API calls: 0")
            print("=" * 70)
        return 2

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_human(report)
    return 0 if report["status"] in {"AUTHORIZATION_REQUIRED", "COMMITTED"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
