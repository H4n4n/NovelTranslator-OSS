"""One exact experimental request boundary; no credential or network on import."""
import copy
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from experimental_goat_policy import (ExperimentError, OPERATIONS, PROVIDER, canonical,
    digest, external_path, local_path, text_sha)
from noveltranslator_core import atomic_write_json
from production_failure_classification import NonProviderProductionError
from production_paid_request_journal import (load_matching_journal, request_identity,
    paid_request_journal_path, run_journaled_paid_request)


class ExperimentalRequestError(NonProviderProductionError):
    failure_kind = "local_safety"


def field(value, key, default=None):
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def raw_record(raw):
    """Retain bounded SDK fields before model/usage acceptance; never exception text."""
    usage = field(raw, "usage")
    data = None if usage is None else {key:field(usage, key) for key in
                                       ("prompt_tokens", "completion_tokens", "total_tokens")}
    if data is not None:
        details = field(usage, "prompt_tokens_details")
        if details is not None:
            data["prompt_tokens_details"] = {"cached_tokens": field(details, "cached_tokens")}
    choices = field(raw, "choices")
    return {"id": field(raw, "id"), "model": field(raw, "model"), "usage": data,
        "choices": [{"finish_reason":field(x, "finish_reason"),
                     "content":field(field(x, "message"), "content")} for x in choices]
                     if isinstance(choices, (list, tuple)) else None}


def normalize_request(model, operation, kwargs, *, probe_effort=None):
    request = copy.deepcopy(kwargs)
    marker = request.pop("_experimental_operation", operation)
    if marker != operation or operation not in OPERATIONS | {"capability_probe"}:
        raise ExperimentError("request operation drift")
    if set(request) - {"model", "messages", "max_tokens", "extra_body"}:
        raise ExperimentError("unsupported request fields")
    if request.get("model") != model.provider_model:
        raise ExperimentError("exact experimental model required")
    messages = request.get("messages")
    if (not isinstance(messages, list) or not messages or any(not isinstance(m, dict)
        or set(m) != {"role", "content"} or m["role"] not in {"system", "user", "assistant"}
        or not isinstance(m["content"], str) for m in messages)):
        raise ExperimentError("invalid messages")
    if request.get("extra_body", {"thinking":{"type":"disabled"}}) != {"thinking":{"type":"disabled"}}:
        raise ExperimentError("unsupported legacy extension")
    requested_ceiling = request.get("max_tokens")
    if type(requested_ceiling) is not int or requested_ceiling < 1:
        raise ExperimentError("explicit completion ceiling required")
    if operation == "capability_probe":
        if probe_effort not in model.allowed_efforts or requested_ceiling > 64 or messages != [{"role":"user", "content":"Reply with OK."}]:
            raise ExperimentError("probe outside micro-probe contract")
        effort = probe_effort
    else:
        if requested_ceiling > model.ceiling(operation):
            raise ExperimentError("request ceiling exceeds experiment policy")
        effort = model.effort(operation)
        # Model ceilings are maxima; preserve the engine-selected smaller bound.
    return {"model": model.provider_model, "messages": messages, "reasoning_effort": effort,
            "max_completion_tokens": requested_ceiling}


def accept_response(model, raw, wire, *, prompt_upper):
    if raw.get("model") != model.provider_model:
        raise ExperimentalRequestError("returned model must match exactly")
    usage = raw.get("usage")
    if not isinstance(usage, dict):
        raise ExperimentalRequestError("provider usage missing")
    prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
    if any(type(x) is not int or x < 0 for x in (prompt, completion)):
        raise ExperimentalRequestError("malformed provider usage")
    total = usage.get("total_tokens")
    if total is not None and (type(total) is not int or total != prompt+completion):
        raise ExperimentalRequestError("invalid total usage")
    exposed = "prompt_tokens_details" in usage
    cached = usage["prompt_tokens_details"].get("cached_tokens") if exposed else 0
    if type(cached) is not int or not 0 <= cached <= prompt:
        raise ExperimentalRequestError("malformed cache usage")
    if prompt > prompt_upper or completion > wire["max_completion_tokens"]:
        raise ExperimentalRequestError("usage exceeds reserved token envelope")
    choices = raw.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise ExperimentalRequestError("exactly one choice required")
    content, finish = choices[0]["content"], choices[0]["finish_reason"]
    if not isinstance(content, str) or finish not in {"stop", "length"}:
        raise ExperimentalRequestError("invalid content or finish reason")
    ce = model.charge(prompt, completion, cached)
    stats = dict(prompt_tokens=prompt, completion_tokens=completion, cache_hit_tokens=cached,
        cache_miss_tokens=prompt-cached, cached_tokens_exposed=exposed,
        cost_usd=float(ce), rate_correct_cost_usd=float(ce), cost_credit_equivalent=str(ce),
        pricing_version=model.sha256, pricing_period="experiment_flat",
        pricing_unit="usd_credit_equivalent_per_1m_tokens", provider=PROVIDER,
        accounting="experimental_command_code_credit_equivalent", provider_invoice_claimed=False,
        cache_hit_rate_per_million=float(model.cache_rate), cache_miss_rate_per_million=float(model.input_rate),
        output_rate_per_million=float(model.output_rate), model_policy_sha256=model.sha256)
    return dict(content=content, finish_reason=finish, stats=stats,
        provider_response_model=raw["model"], provider_response_id=raw.get("id"))


class ExperimentalRuntime:
    def __init__(self, *, root, model, budget, client, plan_sha256):
        self.root, self.model, self.budget = external_path(root, [Path(__file__).parent]), model, budget
        self.client, self.plan_sha256 = client, plan_sha256
        self.calls = []
        self.before_send = None
        if budget.root != self.root or budget.identity["plan_sha256"] != plan_sha256 or budget.model != model:
            raise ExperimentError("runtime budget/configuration mismatch")

    def identity(self, operation, track, chapter, profile, source, kwargs, translation="", *, probe_effort=None):
        wire = normalize_request(self.model, operation, kwargs, probe_effort=probe_effort)
        identity = request_identity(operation_kind=operation, novel_id=track, chapter=chapter,
            provider=PROVIDER, model=self.model.provider_model, profile_id=profile,
            source_sha256=text_sha(source), translation_sha256=text_sha(translation) if translation else "",
            request_payload=wire)
        identity.update(experiment_plan_sha256=self.plan_sha256, model_policy_sha256=self.model.sha256)
        identity["operation_id"] = digest({k:v for k,v in identity.items() if k != "operation_id"})
        return identity, wire

    def request(self, operation, track, chapter, profile, source, kwargs, *, translation="", journal_path=None, probe_effort=None):
        identity, wire = self.identity(operation, track, chapter, profile, source, kwargs, translation,
                                       probe_effort=probe_effort)
        path = local_path(journal_path or paid_request_journal_path(
            self.root / "tracks" / track / "data/provider_request_journal", operation, chapter))
        if self.root not in path.parents:
            raise ExperimentalRequestError("journal outside experiment")
        with self.budget.locked():
            record = load_matching_journal(path, identity)
            reservation = self.budget.document["records"].get(identity["operation_id"])
            if reservation and record is None:
                raise ExperimentalRequestError("missing journal for existing reservation")
            if record and record["status"] in {"started", "uncertain", "response_recorded"}:
                self.budget.require(identity, path)
            if record and record["status"] == "planned" and reservation:
                # Crash after reserve but before STARTED: refuse until explicit evidence review.
                raise ExperimentalRequestError("orphan PLANNED reservation blocks send")

            @contextmanager
            def authorize():
                if self.before_send is not None:
                    self.before_send()
                self.budget.reserve(identity, path, track, 0 if operation == "capability_probe" else chapter, wire)
                try:
                    yield send
                except BaseException:
                    saved = load_matching_journal(path, identity)
                    if saved and saved["status"] == "planned":
                        self.budget.release_presend(identity)
                    raise

            def send():
                if self.client is None:
                    raise ExperimentalRequestError("transport unavailable")
                try:
                    return self.client.chat.completions.create(**copy.deepcopy(wire))
                except BaseException:
                    # Secrets in provider error strings must not reach durable journal errors.
                    raise ExperimentalRequestError("experimental transport outcome ambiguous") from None

            result = run_journaled_paid_request(journal_path=path, identity=identity,
                save_json=atomic_write_json, prepare_attempt=lambda:{"plan_sha256":self.plan_sha256},
                send_once=send, authorize_send=authorize, normalize_response=lambda raw, _:raw_record(raw))
            reservation = self.budget.require(identity, path)
            envelope = accept_response(self.model, result["response"], wire, prompt_upper=reservation["prompt_upper"])
            saved = load_matching_journal(path, identity)
            self.budget.settle(identity, path, envelope["stats"]["cost_credit_equivalent"], saved["response_sha256"])
            envelope["stats"]["request_started_utc"] = saved["created_utc"]
            result["response"] = envelope
            self.calls.append({"operation":operation, "track":track, "chapter":chapter,
                               "wire":copy.deepcopy(wire), "replayed":result["replayed"]})
            return result


def install_experimental_runtime(module, runtime, track_root):
    """Module-local interception of all paid surfaces, retaining engine domain logic."""
    model = runtime.model
    root = local_path(track_root)
    if root != runtime.root / "tracks" / module.ACTIVE_NOVEL_CONFIG.id:
        raise ExperimentError("engine track root mismatch")
    if getattr(module, "_experimental_runtime", None):
        raise ExperimentError("runtime already installed")
    track, profile = module.ACTIVE_NOVEL_CONFIG.id, module.ACTIVE_PROFILE.id
    stack = []
    policies = module.current_production_operation_policy()

    def controls(operation, *, ceiling=None):
        model.effort(operation)
        # Keep existing bounded structured extraction protocol at 4K/8K/5K.
        tokens = policies.for_operation(operation).max_tokens
        if ceiling is not None and ceiling != tokens:
            raise ExperimentError("engine operation ceiling drift")
        return {"max_tokens": model.ceiling(operation) if operation == "chapter_translation" else tokens,
                "extra_body":{"thinking":{"type":"disabled"}}, "_experimental_operation":operation}

    def identity(operation, chapter, source, kwargs, *, translation_text=""):
        return runtime.identity(operation, track, chapter, profile, source, kwargs, translation_text)[0]

    def journaled(operation, chapter, source, kwargs, *, translation_text="", journal_path=None):
        if operation not in OPERATIONS:
            raise ExperimentalRequestError("experimental recovery/fallback operation forbidden")
        expected_chapter = module.progress["last_completed"] if operation.startswith("periodic_audit") else module.progress["next_chapter"]
        if chapter != expected_chapter or source != module.chapter_map[chapter]["text"]:
            raise ExperimentalRequestError("request must bind exact next chapter/source")
        return runtime.request(operation, track, chapter, profile, source, kwargs,
                               translation=translation_text, journal_path=journal_path)

    def auxiliary(**kwargs):
        if not stack:
            raise ExperimentalRequestError("auxiliary request lacks chapter context")
        chapter, context, counters, scope = stack[-1]
        operation = kwargs.get("_experimental_operation")
        model.effort(operation)
        # Fixed invocation ordinal, not request hash: changed body must fail in the same slot.
        ordinal = counters.get(operation, 0)
        counters[operation] = ordinal + 1
        path = root / "data/provider_request_journal" / f"{chapter:04d}.{operation}.{scope}.context{ordinal:04d}.json"
        result = journaled(operation, chapter, module.chapter_map[chapter]["text"], kwargs, journal_path=path)
        env = result["response"]
        return NS(model=model.provider_model, id=env["provider_response_id"],
            choices=[NS(finish_reason=env["finish_reason"], message=NS(content=env["content"]))],
            usage=NS(_experimental_stats=copy.deepcopy(env["stats"])))

    def usage(usage, *args):
        if not hasattr(usage, "_experimental_stats"):
            raise ExperimentalRequestError("usage lacks experimental binding")
        return copy.deepcopy(usage._experimental_stats)

    def saved_stats(stats):
        if not stats.get("prompt_tokens", 0) and not stats.get("completion_tokens", 0) and not stats.get("cost_usd", 0):
            return copy.deepcopy(stats)
        if stats.get("pricing_version") != model.sha256:
            raise ExperimentalRequestError("saved experimental rate card mismatch")
        return copy.deepcopy(stats)

    def translation_stage(*, policy, initial_kwargs, request, **unused):
        from production_translation_recovery import TranslationStageResult
        result = request("chapter_translation", initial_kwargs, "")
        env = result["response"]
        return TranslationStageResult(content=env["content"], stats=env["stats"], elapsed_seconds=0,
            finish_reason=env["finish_reason"], initial=result,
            failure="experimental_translation_length_terminal" if env["finish_reason"] == "length" else
                    "blank_visible_content" if not env["content"].strip() else "")

    original_write = module.atomic_write_json
    def write_bound(path, value):
        target = local_path(path)
        if root not in target.parents:
            raise ExperimentalRequestError("engine write outside isolated track")
        if target.parent in {root / "output/pending", root / "output/qa"}:
            chapter = int(target.stem)
            value = copy.deepcopy(value)
            value["experimental_provenance"] = {"plan_sha256":runtime.plan_sha256,
                "model_policy_sha256":model.sha256, "provider_model":model.provider_model,
                "profile":profile, "novel_id":track}
            if target.parent == root / "output/qa":
                pending_text = root / "output/pending" / f"{chapter:04d}.md"
                value["experimental_translation_sha256"] = text_sha(pending_text.read_text(encoding="utf-8"))
                value["experimental_source_sha256"] = text_sha(module.chapter_map[chapter]["text"])
        return original_write(path, value)
    module.atomic_write_json = write_bound
    module.MODEL = model.provider_model
    module.client = None  # Every legacy raw client surface is poisoned.
    module.api_key = None
    module.AUTO_OFF_PEAK_ONLY = False
    module.operation_request_controls = controls
    module._chat_request_identity = identity
    module.journaled_chat_request = journaled
    module.call_api_with_retries = auxiliary
    module.send_api_request_once = auxiliary
    module.usage_and_cost = usage
    module.usage_and_cost_for_pricing = usage
    module.reprice_saved_stats_if_needed = saved_stats
    module.run_translation_stage = translation_stage
    from experimental_goat_accounting import install_accounting
    install_accounting(module,model,runtime.plan_sha256)
    module._experimental_runtime = runtime
    runtime.before_send = module.wait_for_api_permission
    import builtins
    def experimental_print(*args, **kwargs):
        values = [x.replace("V4 Pro rates", "Experimental CE rates")
                   .replace("API cost", "CE (provider_invoice_claimed=false)")
                   .replace("Calculated API cost", "Calculated CE (provider_invoice_claimed=false)")
                   if isinstance(x,str) else x for x in args]
        builtins.print(*values, **kwargs)
    module.print = experimental_print
    # Bind exact policy into state/review checkpoints, including structured controls.
    from production_saved_checkpoint_identity import _finalize_identity
    for name in ("current_state_checkpoint_identity", "current_review_checkpoint_identity"):
        original = getattr(module, name)
        def bind(original):
            def wrapped(*args, **kwargs):
                value = dict(original(*args, **kwargs))
                value.pop("identity_sha256", None)
                value["experimental_plan_sha256"] = runtime.plan_sha256
                value["experimental_model_policy"] = model.to_dict()
                return _finalize_identity(value)
            return wrapped
        setattr(module, name, bind(original))
    # Process context is the outermost stable ordinal scope. Audits get a separate scope.
    for name, context in (("process_chapter", "chapter_glossary_review"), ("run_periodic_audit", "periodic_audit")):
        original = getattr(module, name)
        def wrap(original, context):
            def wrapped(*args, **kwargs):
                chapter = (args[1] if context == "periodic_audit" and len(args) > 1 else
                           args[0] if args else kwargs.get("chapter_number", kwargs.get("end_chapter")))
                if context != "periodic_audit":
                    pending = root / "output/pending" / f"{chapter:04d}.json"
                    if pending.exists():
                        saved = __import__("json").loads(pending.read_text(encoding="utf-8"))
                        expected = {"plan_sha256":runtime.plan_sha256, "model_policy_sha256":model.sha256,
                            "provider_model":model.provider_model, "profile":profile, "novel_id":track}
                        if saved.get("experimental_provenance") != expected:
                            raise ExperimentalRequestError("pending experimental provenance drift")
                stack.append((chapter, context, {}, "chapter"))
                try:
                    return original(*args, **kwargs)
                finally:
                    stack.pop()
            return wrapped
        setattr(module, name, wrap(original, context))
    # Audit chunks use stable domain identity rather than the ordinal of unfinished chunks.
    import inspect
    for name in ("request_periodic_audit_chunk", "repair_periodic_audit_chunk", "correct_periodic_audit_chunk_semantics"):
        original = getattr(module, name)
        def audit_scope(original, name):
            signature = inspect.signature(original)
            def wrapped(*args, **kwargs):
                if not stack:
                    raise ExperimentalRequestError("audit helper outside audit lifecycle")
                metadata = signature.bind(*args, **kwargs).arguments["metadata"]
                chapter, context, _, _ = stack[-1]
                scope = "audit_" + text_sha(name + ":" + metadata["chunk_id"])
                stack.append((chapter, context, {}, scope))
                try:
                    return original(*args, **kwargs)
                finally:
                    stack.pop()
            return wrapped
        setattr(module, name, audit_scope(original, name))
    return runtime
