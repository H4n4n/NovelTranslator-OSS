"""Production-owned GOAT adapter around the existing sequential lifecycle.

Raw physical responses are journaled before provider acceptance. There is no
second engine, fallback, retry, credential read or client construction here.
"""
import copy
import hashlib
import json
import math
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

from benchmark_longitudinal_g7c_stage1_integration import (
    CONTEXT_ENTRYPOINTS, DIRECT_LEGACY_OPERATION_MAP,
)
from benchmark_longitudinal_g7j_reasoning_budget import (
    reasoning_usage_diagnostics,
)
from production_goat_policy import (translation_completion_budget, classify_translation_response,
    classify_auxiliary_response, WIRE_POLICY_REVISION)
from benchmark_longitudinal_g7d_accounting_policy import pricing_snapshot
from benchmark_longitudinal_provider import build_provider_request_identity
from command_code_provider_transport import (
    accept_command_code_response, account_command_code_usage,
)
from benchmark_longitudinal_provider import normalize_provider_response
from production_failure_classification import NonProviderProductionError
from production_paid_request_journal import (
    run_journaled_paid_request, request_identity, paid_request_journal_path,
    load_matching_journal, _updated_record, _sha256_json, inspect_paid_request_journals,
)
from production_provider_runtime import provider_identity
from production_provider_contract import PROVIDER_COMMAND_CODE, COMMAND_CODE_DEEPSEEK_V41_FLASH

# PR140 domain operations retain their fixed domain-owned journal paths.
DOMAIN_OPERATIONS = {
    "strict_terminology_micro_repair": "terminology_repair",
    "strict_terminology_micro_repair_correction": "terminology_repair",
    "residual_cjk_micro_repair": "terminology_repair",
    "residual_cjk_micro_repair_correction": "terminology_repair",
    "glossary_candidate_conflict_adjudication": "glossary_review",
    "state_extraction_recovery": "state_extraction",
    "generic_json_repair": "state_json_repair",
    "chapter_glossary_review": "glossary_review",
    "chapter_glossary_review_json_repair": "glossary_review",
    "chapter_glossary_review_schema_correction": "glossary_review",
    "chapter_glossary_review_semantic_correction": "glossary_review",
    "chapter_glossary_review_semantic_correction_retry": "glossary_review",
    "periodic_audit": "periodic_audit",
    "periodic_audit_json_repair": "periodic_audit",
    "periodic_audit_semantic_correction": "periodic_audit",
}


class GoatRuntimeError(NonProviderProductionError):
    failure_kind = "control"


class GoatAllowanceExhausted(GoatRuntimeError):
    deterministic_local_budget_refusal = True


def normalize_production_request(logical, kwargs):
    request = copy.deepcopy(kwargs)
    if request.get("model") != "deepseek-flash":
        raise GoatRuntimeError("GOAT legacy request logical model drift")
    # Engine logical selection stays independent from the transport wire model.
    if logical == "translation" and (set(request) != {"model", "messages", "max_tokens", "extra_body"}
            or type(request.get("max_tokens")) is not int or request["max_tokens"] != 12000
            or request.get("extra_body") != {"thinking": {"type": "disabled"}}):
        raise GoatRuntimeError("GOAT translation requires exact reviewed legacy baseline")
    from production_goat_auxiliary_policy import domain_intents
    from benchmark_longitudinal_capability_results import verified_reasoning_capability
    from benchmark_longitudinal_provider import build_provider_request
    ceiling = request.pop("max_tokens", None)
    extra = request.pop("extra_body", None)
    if ("max_completion_tokens" in request or "reasoning_effort" in request
        or extra not in (None, {"thinking": {"type": "disabled"}})
        or type(ceiling) is not int
        or (logical != "translation" and ceiling not in domain_intents(logical))):
        raise GoatRuntimeError("GOAT caller controls differ from domain request intent")
    wire = build_provider_request(model=COMMAND_CODE_DEEPSEEK_V41_FLASH,
        messages=request.pop("messages", None), max_output_tokens=(
            translation_completion_budget(logical_operation=logical, requested_tokens=ceiling,
                reasoning_effort="low") if logical == "translation" else ceiling),
        reasoning_capability=verified_reasoning_capability(COMMAND_CODE_DEEPSEEK_V41_FLASH),
        reasoning_level="low", request_extras={key: value for key, value in request.items() if key != "model"})
    if logical != "translation":
        # Domain output intent is validated above, never a physical GOAT ceiling.
        wire.pop("max_completion_tokens")
    return wire


def _field(value, key, default=None):
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def raw_response_record(raw):
    """Serialize transport evidence without accepting model, shape or usage."""
    usage = _field(raw, "usage")
    fields = ("prompt_tokens", "completion_tokens", "total_tokens")
    data = None if usage is None else {key: _field(usage, key) for key in fields}
    if data is not None:
        for key, token in (("prompt_tokens_details", "cached_tokens"),
                           ("completion_tokens_details", "reasoning_tokens")):
            details = _field(usage, key)
            if details is not None:
                data[key] = {token: _field(details, token)}
    choices = _field(raw, "choices")
    saved_choices = choices
    if isinstance(choices, (list, tuple)):
        saved_choices = [{"finish_reason": _field(item, "finish_reason"),
                          "message": {"content": _field(_field(item, "message"), "content")}}
                         for item in choices]
    return {"id": _field(raw, "id"), "model": _field(raw, "model"),
            "choices": saved_choices, "usage": data}


class GoatBudget:
    """One invocation's physical-send and G7D credit-equivalent allowance."""
    def __init__(self, max_api_calls, max_credit_equivalent_usd):
        if type(max_api_calls) is not int or max_api_calls < 1:
            raise GoatRuntimeError("GOAT physical-send ceiling must be positive")
        if (isinstance(max_credit_equivalent_usd, bool)
            or not isinstance(max_credit_equivalent_usd, (int, float))
            or not math.isfinite(max_credit_equivalent_usd) or max_credit_equivalent_usd <= 0):
            raise GoatRuntimeError("GOAT credit-equivalent ceiling must be finite and positive")
        self.max_api_calls = max_api_calls
        self.max_cost_usd = float(max_credit_equivalent_usd)
        self.api_calls = 0
        self.actual_cost_usd = 0.0
        self.records = []
        self.response_records = []
        self.pre_send_refusals = []
        self.blocked = False
        self.unaccounted_provider_response = False

    @contextmanager
    def authorize(self, request, started):
        rates = pricing_snapshot(COMMAND_CODE_DEEPSEEK_V41_FLASH, started)
        prompt_upper = len(json.dumps(request, ensure_ascii=False).encode("utf-8")) + 8192
        from production_goat_auxiliary_policy import completion_exposure
        try:
            exposure = completion_exposure(request, prompt_upper=prompt_upper)
        except ValueError as exc:
            self.pre_send_refusals.append({"kind": "authoritative_hard_bound_missing_or_invalid"})
            raise GoatRuntimeError("GOAT uncapped authorization needs architecture review: no reviewed hard bound") from exc
        bound = (exposure["prompt_upper"] * rates["cache_miss_rate_per_million"]
                 + exposure["completion_upper"] * rates["output_rate_per_million"]) / 1_000_000
        if self.blocked or self.api_calls >= self.max_api_calls or bound > self.max_cost_usd - self.actual_cost_usd + 1e-12:
            self.pre_send_refusals.append({"kind": "deterministic_local_budget_refusal"})
            raise GoatAllowanceExhausted("GOAT invocation send/credit-equivalent ceiling refuses request")
        self.authorized_exposure = {**exposure, "credit_equivalent_upper": bound}
        yield

    def begin(self, digest):
        self.api_calls += 1
        self.records.append({"call": self.api_calls, "request_sha256": digest, "status": "started",
            "authorization_exposure": copy.deepcopy(self.authorized_exposure)})

    def account(self, stats):
        self.actual_cost_usd += stats["cost_usd"]
        self.records[-1].update({"status": "success", "stats": copy.deepcopy(stats)})
        exposure = self.records[-1]["authorization_exposure"]
        if (stats["prompt_tokens"] > exposure["prompt_upper"]
            or stats["completion_tokens"] > exposure["completion_upper"]
            or stats["cost_usd"] > exposure["credit_equivalent_upper"] + 1e-12):
            self.blocked = True
            raise GoatRuntimeError("GOAT usage exceeded authorized conservative exposure; architecture review required")
        if self.actual_cost_usd > self.max_cost_usd + 1e-12:
            self.blocked = True
            raise GoatRuntimeError("GOAT accepted usage exceeded credit-equivalent allowance")


def install_goat_runtime(module, *, provider_client, budget=None, clock=None, batch_context=None):
    """Install after provider-neutral initialization; client explicitly supplied."""
    if not module._RUNTIME_INITIALIZED:
        raise GoatRuntimeError("provider-neutral state must initialize first")
    if hasattr(module, "_production_goat_runtime"):
        raise GoatRuntimeError("GOAT runtime already installed")
    if batch_context is not None:
        # No flags, environment, module attributes, duck-typed authorizers or
        # executable entrypoints activate B2B2. Its sole context always refuses.
        from production_goat_batch_boundary import BatchNoSendContext
        if type(batch_context) is not BatchNoSendContext:
            raise GoatRuntimeError("explicit exact B2B2 NO_SEND context required")
        batch_context.validate_runtime_locked(module)
    identity = provider_identity(PROVIDER_COMMAND_CODE, module.MODEL)
    clock = clock or (lambda: datetime.now(timezone.utc))
    def effective_inventory():
        raw = inspect_paid_request_journals(module.PAID_REQUEST_JOURNAL_DIR)
        if raw["errors"]:
            return raw
        from pathlib import Path
        from novel_project import load_novel_project
        from production_uncertain_request_disposition import effective_paid_request_inventory, registry_path
        journal_root = Path(module.PAID_REQUEST_JOURNAL_DIR).absolute()
        root = journal_root.parent.parent
        # Empty raw ambiguity is insufficient: surviving registry evidence is
        # a continuing obligation to prove the original UNCERTAIN bytes.
        registry = registry_path(SimpleNamespace(root=root, data_dir=root / "data"), 1).parent
        if not raw["uncertain"] and not registry.exists():
            return raw
        project = load_novel_project(module.ACTIVE_NOVEL_CONFIG.id, repo_root=root.parent.parent)
        if (project.data_dir / "provider_request_journal").absolute() != journal_root:
            raise GoatRuntimeError("GOAT ambiguity inventory novel path mismatch")
        return effective_paid_request_inventory(project, inventory=raw)
    stack = []
    review_request_scope = []
    audit_request_scope = []
    context_operations = {}
    review_recovery_authorization = {}
    translation_requests = {}
    state_extraction_scope = []
    originals = {name: getattr(module, name) for name in CONTEXT_ENTRYPOINTS}

    def operation_for(kind):
        from production_uncertain_request_disposition import KIND as NO_RECORD_STATE
        if kind == NO_RECORD_STATE:
            return "state_extraction"
        from production_goat_state_terminal_recovery import KIND as TERMINAL_STATE
        if kind == TERMINAL_STATE:
            return "state_extraction"
        from production_goat_state_policy_migration import KIND as STATE_MIGRATION
        if kind == STATE_MIGRATION:
            return "state_extraction"
        from production_goat_cap_recovery import KIND
        if kind == KIND:
            return "translation"
        if kind in DIRECT_LEGACY_OPERATION_MAP:
            return DIRECT_LEGACY_OPERATION_MAP[kind]
        from production_goat_glossary_review_recovery import KIND as REVIEW_RECOVERY
        if kind == REVIEW_RECOVERY:
            return "glossary_review"
        if kind in DOMAIN_OPERATIONS:
            return DOMAIN_OPERATIONS[kind]
        if kind in context_operations:
            return context_operations[kind]
        raise GoatRuntimeError("unmapped GOAT physical operation: " + str(kind))

    def journal_identity(kind, chapter, source, kwargs, *, translation_text=""):
        logical = operation_for(kind)
        wire = normalize_production_request(logical, kwargs)
        from production_uncertain_request_disposition import KIND as NO_RECORD_STATE, journal_identity as disposition_identity
        if kind == NO_RECORD_STATE:
            proof = getattr(module, "_goat_uncertain_state_proof", None)
            if (not proof or proof["chapter"] != chapter or proof["provider_policy"] != identity
                or proof["source_sha256"] != hashlib.sha256(source.encode("utf-8")).hexdigest()
                or proof["translation_sha256"] != hashlib.sha256(translation_text.encode("utf-8")).hexdigest()):
                raise GoatRuntimeError("uncertain recovery requires explicit scoped disposition proof")
            return disposition_identity(proof, wire)
        from production_goat_state_terminal_recovery import KIND as TERMINAL_STATE, journal_identity as terminal_identity
        if kind == TERMINAL_STATE:
            proof = getattr(module, "_goat_terminal_state_proof", None)
            if (not proof or proof["chapter"] != chapter or proof["provider_policy"] != identity
                or proof["source_sha256"] != hashlib.sha256(source.encode("utf-8")).hexdigest()
                or proof["translation_sha256"] != hashlib.sha256(translation_text.encode("utf-8")).hexdigest()):
                raise GoatRuntimeError("terminal state recovery requires exact explicit planner proof")
            return terminal_identity(proof, wire)
        from production_goat_state_policy_migration import KIND as STATE_MIGRATION, journal_identity as migration_identity, text_sha
        if kind == STATE_MIGRATION:
            proof = getattr(module, "_goat_state_migration_proof", None)
            if (not proof or proof["chapter"] != chapter or proof["new_policy"] != identity
                or proof["source_sha256"] != text_sha(source)
                or proof["translation_sha256"] != text_sha(translation_text)):
                raise GoatRuntimeError("state migration requires exact scoped planner proof")
            return migration_identity(proof, wire)
        result = request_identity(operation_kind=kind, novel_id=module.ACTIVE_NOVEL_CONFIG.id,
            chapter=chapter, provider=PROVIDER_COMMAND_CODE, model=identity["wire_model"],
            profile_id=module.ACTIVE_PROFILE.id,
            source_sha256=hashlib.sha256(source.encode("utf-8")).hexdigest(),
            translation_sha256=hashlib.sha256(translation_text.encode("utf-8")).hexdigest() if translation_text else "",
            request_payload={"wire_request": wire, "provider_policy": identity})
        from production_goat_cap_recovery import KIND, recovery_binding
        if kind == KIND:
            initial_binding = journal_identity("chapter_translation", chapter, source, kwargs)
            record = load_matching_journal(paid_request_journal_path(module.PAID_REQUEST_JOURNAL_DIR,
                "chapter_translation", chapter), initial_binding)
            if translation_text != recovery_binding(record, wire):
                raise GoatRuntimeError("GOAT recovery binding lacks exact initial response proof")
            result["reasoning_saturation_recovery"] = json.loads(translation_text)
            result["recovery_controls"] = {key: wire[key] for key in ("model", "reasoning_effort", "max_completion_tokens")}
            inspection = effective_inventory()
            if inspection["errors"] or inspection["uncertain"]:
                raise GoatRuntimeError("GOAT recovery blocked by ambiguous journal state")
        result["provider_policy"] = copy.deepcopy(identity)
        result["wire_request_sha256"] = build_provider_request_identity(request_body=wire).request_sha256
        return result

    def journaled(kind, chapter, source, kwargs, *, translation_text="", journal_path=None,
                  original_review=False, recovery_proof=None, outer_binding=None, replay_only=False):
        logical = operation_for(kind)
        wire = normalize_production_request(logical, kwargs)
        transport = build_provider_request_identity(request_body=wire)
        binding = journal_identity(kind, chapter, source, kwargs, translation_text=translation_text)
        from production_goat_glossary_review_recovery import KIND as REVIEW_KIND
        if kind == REVIEW_KIND and (recovery_proof is None
                or review_recovery_authorization.get(chapter) != recovery_proof):
            raise GoatRuntimeError("glossary recovery requires exact scoped original authorization")
        if recovery_proof is not None:
            if kind != REVIEW_KIND:
                raise GoatRuntimeError("glossary proof cannot authorize another operation")
            from production_goat_glossary_review_recovery import journal_identity as review_identity
            binding = review_identity(kind=kind, novel_id=module.ACTIVE_NOVEL_CONFIG.id,
                chapter=chapter, profile_id=module.ACTIVE_PROFILE.id,
                source_sha256=hashlib.sha256(source.encode("utf-8")).hexdigest(), wire=wire,
                proof=recovery_proof)
        path = journal_path or paid_request_journal_path(module.PAID_REQUEST_JOURNAL_DIR, kind, chapter)
        if outer_binding is not None:
            binding["auxiliary_domain_binding"] = copy.deepcopy(outer_binding)
        if replay_only:
            recorded_only = load_matching_journal(path, binding)
            if recorded_only is None or recorded_only["status"] != "response_recorded":
                raise GoatRuntimeError("local auxiliary recovery requires exact RESPONSE_RECORDED; no transport")
        # Old exact auxiliary bodies still bind a different full provider policy.
        # Detect that migration boundary before planning, authorization or send.
        from pathlib import Path
        from production_provider_runtime import historical_goat_provider_identity, HistoricalGoatManualReviewRequired
        if Path(path).is_file():
            historical = load_matching_journal(path, {})
            if (identity != historical_goat_provider_identity()
                and historical.get("provider_policy") == historical_goat_provider_identity()):
                raise HistoricalGoatManualReviewRequired()
        attempt = {}

        def prepare():
            if provider_client is None:
                raise GoatRuntimeError("GOAT transport scope is closed")
            # Manual pause still applies; GOAT does not inherit direct peak gates.
            module.wait_for_api_permission()
            value = {"request_started_utc": clock().astimezone(timezone.utc).isoformat()}
            attempt.update(value)
            return value

        def send():
            if getattr(module, "_goat_glossary_recovery_only", False) and logical != "glossary_review":
                raise GoatRuntimeError("dedicated glossary recovery forbids other lifecycle sends")
            if budget is not None:
                budget.begin(transport.request_sha256)
            try:
                return provider_client.chat.completions.create(**copy.deepcopy(wire))
            except BaseException:
                if budget is not None:
                    budget.blocked = True
                raise GoatRuntimeError("GOAT transport outcome uncertain; automatic reissue forbidden") from None

        @contextmanager
        def authorize():
            from production_uncertain_request_disposition import KIND as NO_RECORD_STATE
            uncertain_phase = getattr(module, "_goat_uncertain_state_phase", None)
            if uncertain_phase and not ((uncertain_phase == "state" and kind == NO_RECORD_STATE)
                    or (uncertain_phase == "json_repair" and kind == "state_json_repair")):
                raise GoatRuntimeError("uncertain recovery forbids this physical operation")
            from production_goat_state_terminal_recovery import KIND as TERMINAL_STATE
            terminal_phase = getattr(module, "_goat_terminal_state_phase", None)
            if terminal_phase and not ((terminal_phase == "state" and kind == TERMINAL_STATE)
                    or (terminal_phase == "json_repair" and kind == "state_json_repair")):
                raise GoatRuntimeError("terminal state recovery forbids this physical operation")
            from production_goat_state_policy_migration import KIND as STATE_MIGRATION
            phase = getattr(module, "_goat_state_migration_phase", None)
            if phase and (logical == "translation" or
                (phase == "state_only" and kind != STATE_MIGRATION) or
                (phase == "post_state" and logical == "state_extraction")):
                raise GoatRuntimeError("state migration scope forbids this physical operation")
            if budget is None:
                raise GoatRuntimeError("GOAT physical send requires explicit credit/send authorization")
            else:
                with budget.authorize(wire, attempt["request_started_utc"]):
                    if uncertain_phase:
                        from production_uncertain_request_disposition import claim_send
                        claim_send(module, kind, binding)
                    yield send

        result = run_journaled_paid_request(journal_path=path, identity=binding,
            save_json=module.atomic_write_json, prepare_attempt=prepare, send_once=send,
            authorize_send=authorize,
            observe_request=(None if batch_context is None else
                lambda observation: batch_context.observe_locked(observation, wire)),
            normalize_response=lambda raw, context: {"raw_response": raw_response_record(raw), **context})
        recorded = result["response"]
        accounted = False
        try:
            normalized = normalize_provider_response(recorded["raw_response"], request_identity=transport)
            accepted = accept_command_code_response(transport, normalized)
            stats = account_command_code_usage(identity["wire_model"], accepted.usage, recorded["request_started_utc"])
            stats.update(reasoning_usage_diagnostics(completion_tokens=accepted.usage.completion_tokens,
                reasoning_tokens=accepted.usage.reasoning_tokens,
                reasoning_tokens_exposed=accepted.usage.reasoning_tokens_exposed))
            stats.update({"provider": PROVIDER_COMMAND_CODE, "accounting": identity["accounting"], "provider_invoice_claimed": False,
                          "request_sha256": transport.request_sha256, "pricing_unit": identity["pricing_unit"]})
            envelope = {**recorded, "content": accepted.content, "finish_reason": accepted.finish_reason,
                        "stats": stats, "provider_response_model": accepted.response_model,
                        "provider_response_id": accepted.provider_response_id,
                        "request_sha256": transport.request_sha256, "provider_identity": identity}
            # Older production envelopes omitted these diagnostics. Compare their
            # exact original fields, then enrich from the durable raw response.
            legacy_stats = {key: value for key, value in stats.items() if key not in
                            {"provider", "reasoning_tokens", "reasoning_tokens_exposed", "visible_completion_tokens"}}
            if "stats" in recorded and recorded["stats"] not in (stats, legacy_stats):
                raise GoatRuntimeError("GOAT durable accounting replay drift")
            for field in ("content", "finish_reason", "provider_response_model", "provider_response_id",
                          "request_sha256", "provider_identity"):
                if field in recorded and recorded[field] != envelope[field]:
                    raise GoatRuntimeError("GOAT durable response envelope replay drift: " + field)
            if recorded.get("stats") != stats:
                # Raw response already durable; enrich without altering paid identity.
                record = load_matching_journal(path, binding)
                module.atomic_write_json(path, _updated_record(record, response=envelope,
                    response_sha256=_sha256_json(envelope)))
            if budget is not None and result["api_called"]:
                budget.account(stats)
            accounted = True
            if budget is not None:
                budget.response_records.append({"replayed": result["replayed"], "stats": copy.deepcopy(stats)})
            if logical == "translation":
                diagnostic = classify_translation_response(content=accepted.content,
                    finish_reason=accepted.finish_reason, completion_tokens=stats["completion_tokens"],
                    max_completion_tokens=wire["max_completion_tokens"])
                from production_goat_cap_recovery import eligible_envelope
                if diagnostic["status"] != "ok" and not eligible_envelope(envelope, wire, kind=kind):
                    raise GoatRuntimeError("GOAT translation response rejected: " + diagnostic["status"])
                if diagnostic["status"] != "ok":
                    inspection = effective_inventory()
                    if inspection["errors"] or inspection["uncertain"]:
                        raise GoatRuntimeError("GOAT saturation blocked by ambiguous journal state")
                    # Accounted evidence only: the controller must recover before pending/QA.
                    return {**result, "response": envelope}
                translation_requests[chapter] = transport.request_sha256
            else:
                diagnostic = classify_auxiliary_response(logical_operation=logical, content=accepted.content,
                    finish_reason=accepted.finish_reason, completion_tokens=stats["completion_tokens"],
                    reasoning_tokens=stats.get("reasoning_tokens"))
                from production_goat_glossary_review_recovery import eligible_envelope as eligible_review
                if diagnostic["fail_closed"] and original_review and eligible_review(envelope, wire, original=True):
                    inspection = effective_inventory()
                    if inspection["errors"] or inspection["uncertain"]:
                        raise GoatRuntimeError("GOAT glossary recovery blocked by ambiguous journals")
                    return {**result, "response": envelope}
                audit_split = (logical == "periodic_audit" and outer_binding is not None
                    and outer_binding["kind"] == "audit" and accepted.finish_reason == "length")
                from production_goat_state_policy_migration import KIND as STATE_MIGRATION
                state_controller = logical == "state_extraction" and (state_extraction_scope or kind == STATE_MIGRATION) and (
                    accepted.finish_reason == "length" or accepted.finish_reason == "stop")
                from production_goat_state_terminal_recovery import KIND as TERMINAL_STATE
                terminal_controller = kind == TERMINAL_STATE and getattr(module, "_goat_terminal_state_proof", None)
                from production_uncertain_request_disposition import KIND as NO_RECORD_STATE
                uncertain_controller = kind == NO_RECORD_STATE and getattr(module, "_goat_uncertain_state_proof", None)
                if diagnostic["fail_closed"] and not audit_split and not state_controller and not terminal_controller and not uncertain_controller:
                    raise GoatRuntimeError("GOAT auxiliary response rejected: " + diagnostic["status"])
            return {**result, "response": envelope,
                "wire_completion_policy": ({"revision": WIRE_POLICY_REVISION,
                    "auxiliary_completion_policy": "provider_unbounded", "auxiliary_completion_tokens": None}
                    if logical != "translation" else {"max_completion_tokens": wire["max_completion_tokens"]})}
        except Exception as exc:
            if budget is not None:
                if not accounted:
                    # Applies to fresh sends AND local replay, without counting
                    # a replay as a new physical attempt or fabricating usage.
                    budget.unaccounted_provider_response = True
                budget.blocked = True
                if result["api_called"] and budget.records[-1]["status"] == "started":
                    budget.records[-1]["status"] = "missing_usage_unaccounted_response"
            raise GoatRuntimeError("GOAT recorded response failed acceptance/accounting; no reissue") from exc

    def original_glossary_review(kwargs, metadata, translation):
        from production_goat_glossary_review_recovery import (
            KIND, initial_kind, eligible_envelope, recovery_proof as prove_recovery,
        )
        if not stack or stack[-1] != ("glossary_review", metadata["chapter"]):
            raise GoatRuntimeError("original glossary review requires explicit lifecycle context")
        chapter = metadata["chapter"]
        source = module.chapter_map[chapter]["text"]
        wire = normalize_production_request("glossary_review", kwargs)
        kind = initial_kind(wire)
        context_operations[kind] = "glossary_review"
        try:
            expected = journal_identity(kind, chapter, source, kwargs)
            scope = review_request_scope[-1] if review_request_scope else None
            domain_binding = scope["binding"]["identity"]["auxiliary_domain_binding"] if scope else None
            if domain_binding is not None:
                expected["auxiliary_domain_binding"] = copy.deepcopy(domain_binding)
            path = paid_request_journal_path(module.PAID_REQUEST_JOURNAL_DIR, kind, chapter)
            if metadata.get("require_recorded_empty"):
                from production_goat_glossary_review_recovery import prove_initial
                prove_initial(load_matching_journal(path, expected), wire, expected)
            initial = journaled(kind, chapter, source, kwargs, original_review=True,
                outer_binding=domain_binding, replay_only=scope["replay_only"] if scope else False)
            effective = initial
            proof = None
            if eligible_envelope(initial["response"], wire, original=True):
                if budget is None:
                    raise GoatRuntimeError("glossary recovery requires an explicit GoatBudget")
                from production_goat_glossary_review_recovery import pending_binding
                from chapter_glossary_review import CHAPTER_REVIEW_RECOVERY_KEY
                from auxiliary_response_reconciliation import original_review_attempt_marker
                from pathlib import Path
                pending_path = Path(module.PENDING_DIR) / f"{chapter:04d}.json"
                pending_text = (pending_path.with_suffix(".md")).read_text(encoding="utf-8")
                pending_meta = json.loads(pending_path.read_text(encoding="utf-8"))
                domain = pending_meta.get(CHAPTER_REVIEW_RECOVERY_KEY, {})
                if (pending_text != translation or pending_meta.get("stage") != "state_extracted"
                    or pending_meta.get("local_qa", {}).get("commit_allowed") is not True
                    or domain.get("status") != "original_started"
                    or domain.get("input_hash") != metadata["input_hash"]
                    or original_review_attempt_marker(domain.get("original", {}))
                        != {"operation_id": metadata["operation_id"], "attempted": True}
                    or pending_meta.get("state_checkpoint_identity") != module.current_state_checkpoint_identity(chapter, source, translation)):
                    raise GoatRuntimeError("GOAT original pending/checkpoint proof failed")
                metadata = {**metadata, "checkpoint_binding": pending_binding(pending_meta)}
                record = load_matching_journal(path, expected)
                proof = prove_recovery(record, wire, expected, metadata=metadata,
                    translation=translation, journal_path=path)
                review_recovery_authorization[chapter] = copy.deepcopy(proof)
                try:
                    effective = journaled(KIND, chapter, source, copy.deepcopy(kwargs), recovery_proof=proof)
                finally:
                    review_recovery_authorization.pop(chapter, None)
                envelope = effective["response"]
                if envelope["finish_reason"] != "stop" or not envelope["content"].strip():
                    raise GoatRuntimeError("GOAT glossary recovery terminal; no further send")
                recovery_path = paid_request_journal_path(module.PAID_REQUEST_JOURNAL_DIR, KIND, chapter)
                recovered = load_matching_journal(recovery_path, {})
                proof = {**proof, "initial_domain_attempt_existed": True,
                    "initial_accepted_empty_accounted": True, "recovery_authorized": True,
                    "recovery_operation_id": recovered["operation_id"],
                    "recovery_response_sha256": recovered["response_sha256"],
                    "effective_original": "recovered_nonempty_response"}
            envelope = effective["response"]
            stats = (module.combine_stats(initial["response"]["stats"], envelope["stats"])
                     if proof else envelope["stats"])
            if proof:
                stats["glossary_review_accepted_operations"] = [
                    {"operation_id": value["operation_id"], "stats": copy.deepcopy(value["response"]["stats"])}
                    for value in (initial, effective)]
            usage = SimpleNamespace(**stats, _goat_stats=copy.deepcopy(stats))
            return SimpleNamespace(usage=usage, _goat_glossary_recovery=proof,
                choices=[SimpleNamespace(finish_reason=envelope["finish_reason"],
                    message=SimpleNamespace(content=envelope["content"]))])
        finally:
            context_operations.pop(kind, None)

    def auxiliary(**kwargs):
        if not stack:
            raise GoatRuntimeError("GOAT auxiliary request outside an explicit lifecycle context")
        logical, chapter = stack[-1]
        source = module.chapter_map[chapter]["text"]
        wire = normalize_production_request(logical, kwargs)
        digest = build_provider_request_identity(request_body=wire).request_sha256
        kind = "context_" + logical + "_" + digest
        # Context subrequests have deterministic independent slots, as in G7C.
        context_operations[kind] = logical
        try:
            scope = (review_request_scope[-1] if review_request_scope else
                     audit_request_scope[-1] if audit_request_scope else None)
            domain_binding = scope["binding"]["identity"]["auxiliary_domain_binding"] if scope else None
            # Correction's first request must use the already-bound exact slot.
            # Original review JSON repair is reconstructed from its exact paid
            # initial content and frozen repair policy; it owns its own slot.
            replay_only = bool(scope and scope["replay_only"] and scope["kind"] in {"correction", "audit"})
            result = journaled(kind, chapter, source, kwargs, outer_binding=domain_binding, replay_only=replay_only)
        finally:
            context_operations.pop(kind, None)
        envelope = result["response"]
        usage = SimpleNamespace(**envelope["stats"], _goat_stats=copy.deepcopy(envelope["stats"]))
        return SimpleNamespace(id=envelope["provider_response_id"], model=envelope["provider_response_model"],
            usage=usage, choices=[SimpleNamespace(finish_reason=envelope["finish_reason"],
                message=SimpleNamespace(content=envelope["content"]))])

    def usage_stats(usage, _pricing=None):
        if not hasattr(usage, "_goat_stats"):
            raise GoatRuntimeError("unbound GOAT response accounting")
        return copy.deepcopy(usage._goat_stats)

    def saved_stats(stats):
        if stats.get("cost_usd", 0) == 0 and not stats.get("prompt_tokens", 0) and not stats.get("completion_tokens", 0):
            return copy.deepcopy(stats)
        if stats.get("pricing_version") != identity["pricing_version"] or stats.get("pricing_unit") != identity["pricing_unit"]:
            raise GoatRuntimeError("saved GOAT stats accounting policy drift")
        result = copy.deepcopy(stats)
        # Historical saved GOAT checkpoints already bind this exact rate card.
        # Add provider semantics without inventing an unreported subtotal.
        result.setdefault("provider", PROVIDER_COMMAND_CODE)
        result.setdefault("accounting", identity["accounting"])
        result.setdefault("provider_invoice_claimed", False)
        from production_accounting import validate_goat_stats
        validate_goat_stats(result)
        return result

    from production_saved_checkpoint_identity import _finalize_identity
    for name in ("current_state_checkpoint_identity", "current_review_checkpoint_identity"):
        original_identity = getattr(module, name, None)
        if not callable(original_identity):
            continue
        def bind_checkpoint(original):
            def bound(*args, **kwargs):
                from production_goat_policy import bind_auxiliary_checkpoint
                return bind_auxiliary_checkpoint(original(*args, **kwargs), copy.deepcopy(identity))
            return bound
        setattr(module, name, bind_checkpoint(original_identity))
    from production_accounting import combine_goat_stats, validate_progress_accounting
    validate_progress_accounting(module.progress, expected_provider=PROVIDER_COMMAND_CODE)
    original_combine = module.combine_stats
    module.combine_stats = lambda *items: combine_goat_stats(original_combine, *items)
    import builtins
    def provider_print(*args, **kwargs):
        labels = {"V4 Pro": "GOAT public rate card (credit-equivalent; provider_invoice_claimed=false)",
                  "DEEPSEEK NOVEL TRANSLATION": "GOAT NOVEL TRANSLATION",
                  "API cost": "credit-equivalent (provider_invoice_claimed=false)",
                  "Adjudication cost": "Adjudication credit-equivalent (provider_invoice_claimed=false)",
                  "Legacy cumulative project counter": "Cumulative credit-equivalent (provider_invoice_claimed=false)"}
        values = []
        for value in args:
            if isinstance(value, str):
                if value.startswith("Legacy counter note:"):
                    value = "Accounting: frozen public rate card credit-equivalent; provider_invoice_claimed=false."
                for old, new in labels.items():
                    value = value.replace(old, new)
            values.append(value)
        builtins.print(*values, **kwargs)
    module.print = provider_print
    module.client = provider_client
    module.api_key = None
    module.AUTO_OFF_PEAK_ONLY = False
    module._chat_request_identity = journal_identity
    module.journaled_chat_request = journaled
    module.goat_original_glossary_review = original_glossary_review
    module.call_api_with_retries = auxiliary
    module.send_api_request_once = auxiliary
    module.usage_and_cost = usage_stats
    module.usage_and_cost_for_pricing = usage_stats
    module.reprice_saved_stats_if_needed = saved_stats
    original_extract_state = getattr(module, "extract_state", None)
    if callable(original_extract_state):
        def bounded_state_controller(*args, **kwargs):
            state_extraction_scope.append(True)
            try:
                return original_extract_state(*args, **kwargs)
            finally:
                state_extraction_scope.pop()
        module.extract_state = bounded_state_controller
    module.pricing_snapshot = lambda moment=None: pricing_snapshot(identity["wire_model"], moment or clock())
    for name, logical in CONTEXT_ENTRYPOINTS.items():
        original = originals[name]
        def wrap(original, logical):
            def wrapped(*args, **kwargs):
                chapter = args[1] if logical == "periodic_audit" and len(args) >= 2 else (
                    args[0] if args else kwargs.get("chapter_number", kwargs.get("end_chapter")))
                if type(chapter) is not int or chapter < 1:
                    raise GoatRuntimeError("GOAT operation context lacks a chapter")
                stack.append((logical, chapter))
                try:
                    return original(*args, **kwargs)
                finally:
                    stack.pop()
            return wrapped
        setattr(module, name, wrap(original, logical))
    # The outer audit owns attempt identity; GOAT owns exact wire normalization,
    # fixed slot selection, acceptance and accounting. No provider internals leak
    # into glossary_audit, and existing context_<logical>_<wire SHA> slots stay fixed.
    from auxiliary_response_reconciliation import JournalOwnedAuditCallback
    original_chunked_audit = module.run_chunked_audit

    def bind_audit_request(kwargs, record, metadata):
        if not stack or stack[-1] != ("periodic_audit", metadata["audit_window"]["end_chapter"]):
            raise GoatRuntimeError("audit reconciliation requires exact lifecycle chapter context")
        chapter = stack[-1][1]
        source = module.chapter_map[chapter]["text"]
        wire = normalize_production_request("periodic_audit", kwargs)
        kind = "context_periodic_audit_" + build_provider_request_identity(request_body=wire).request_sha256
        context_operations[kind] = "periodic_audit"
        try:
            expected = journal_identity(kind, chapter, source, kwargs)
        finally:
            context_operations.pop(kind, None)
        domain = {key: copy.deepcopy(record[key]) for key in (
            "chunk_id", "kind", "attempt", "operation_id", "model_id_transport_version")}
        domain["request_metadata"] = copy.deepcopy({key: value for key, value in metadata.items()
            if key != "outer_attempt"})
        expected["auxiliary_domain_binding"] = domain
        return {"journal_path": str(paid_request_journal_path(module.PAID_REQUEST_JOURNAL_DIR, kind, chapter)),
                "identity": expected}

    def wrap_audit_callback(callback, builder):
        class AuditCallback(JournalOwnedAuditCallback):
            def invoke(self, args, binding, *, replay_only):
                # Keep the original callback's request/response logging. Its API
                # boundary is scoped to the already validated exact paid slot.
                audit_request_scope.append({"binding": binding, "kind": "audit", "replay_only": replay_only})
                try:
                    return callback(*args)
                finally:
                    audit_request_scope.pop()
        return AuditCallback(build_request=builder, bind_request=bind_audit_request, send_request=None)

    def journal_owned_audit(*args, **kwargs):
        forwarded = dict(kwargs)
        for name in ("request_chunk", "repair_chunk", "correct_semantic_chunk"):
            callback = forwarded.get(name)
            builder = getattr(callback, "build_request", None)
            if callable(builder):
                forwarded[name] = wrap_audit_callback(callback, builder)
        return original_chunked_audit(*args, **forwarded)

    module.run_chunked_audit = journal_owned_audit
    from auxiliary_response_reconciliation import (
        JournalOwnedAuxiliaryCallback, reconcile_owned_request,
    )

    def journal_owned_chapter_review_callbacks(original_callback, correction_callback):
        def wrap(callback, callback_kind):
            def bind_request(kwargs, record, metadata):
                if not stack or stack[-1] != ("glossary_review", metadata["chapter"]):
                    raise GoatRuntimeError("chapter review reconciliation lacks exact lifecycle context")
                chapter = metadata["chapter"]
                wire = normalize_production_request("glossary_review", kwargs)
                if callback_kind == "original":
                    from production_goat_glossary_review_recovery import initial_kind
                    kind = initial_kind(wire)
                else:
                    kind = "context_glossary_review_" + build_provider_request_identity(request_body=wire).request_sha256
                context_operations[kind] = "glossary_review"
                try:
                    expected = journal_identity(kind, chapter, module.chapter_map[chapter]["text"], kwargs)
                finally:
                    context_operations.pop(kind, None)
                domain = {"operation_id": record["operation_id"], "callback_kind": callback_kind,
                    "metadata": {key: copy.deepcopy(value) for key, value in metadata.items()
                        if key not in {"outer_attempt", "persist_transport_recovery", "require_recorded_empty"}},
                    "json_repair_policy_sha256": _sha256_json({
                        "prompt": module.GENERIC_JSON_REPAIR_PROMPT, "schema": module.TERM_REVIEW_SCHEMA,
                        "controls": module.operation_request_controls("chapter_glossary_review_json_repair")})}
                expected["auxiliary_domain_binding"] = domain
                return {"journal_path": str(paid_request_journal_path(module.PAID_REQUEST_JOURNAL_DIR, kind, chapter)),
                        "identity": expected}

            class ChapterReviewCallback(JournalOwnedAuxiliaryCallback):
                def invoke(self, record, args, *, replay_only):
                    binding = self.prepare(record, args)
                    if binding != record.get("transport_binding"):
                        raise GoatRuntimeError("chapter review transport/request binding mismatch")
                    review_request_scope.append({"binding": binding, "kind": callback_kind, "replay_only": replay_only})
                    try:
                        return callback(*args)
                    finally:
                        review_request_scope.pop()

                def __call__(self, *args):
                    # Legacy original recovery still uses the separately
                    # validated reasoning-empty protocol, never ordinary send.
                    if "outer_attempt" not in args[-1] and args[-1].get("require_recorded_empty") is True:
                        return callback(*args)
                    return self.invoke(args[-1]["outer_attempt"], args, replay_only=False)

                def recover(self, record, args):
                    from auxiliary_response_reconciliation import BOUND_BEFORE_TRANSPORT_VERSION
                    binding = self.prepare(record, args)
                    if binding != record.get("transport_binding"):
                        raise GoatRuntimeError("saved chapter review transport/request binding mismatch")
                    if (load_matching_journal(binding["journal_path"], binding["identity"]) is None
                            and record.get("transport_ownership_version") != BOUND_BEFORE_TRANSPORT_VERSION):
                        # Only the new bound-before-transport protocol proves
                        # absence is pre-send. Historical/empty-output chains
                        # without that ordering proof retain the absolute block.
                        raise GoatRuntimeError("chapter review has no durable transport proof; automatic reissue is blocked")
                    return reconcile_owned_request(binding=binding,
                        load_response=lambda: self.invoke(record, args, replay_only=True))

            return ChapterReviewCallback(build_request=callback.build_request,
                bind_request=bind_request, send_request=None)
        return wrap(original_callback, "original"), wrap(correction_callback, "correction")

    module.journal_owned_chapter_review_callbacks = journal_owned_chapter_review_callbacks
    def release_transport():
        nonlocal provider_client
        provider_client = None

    state = {"provider_identity": identity, "translation_requests": translation_requests,
             "budget": budget, "release_transport": release_transport}
    module._production_goat_runtime = state
    return state
