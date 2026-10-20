"""B2B1 read-only real-journal validators. No executor or send authorization.

Internal *_locked methods require the existing caller-held novel writer lock.
An independent rollback anchor and the pre-STARTED hook are NOT implemented;
successful validation must never be interpreted as permission to send.
"""
import copy
import hashlib
import json
import re
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_CEILING, localcontext
from pathlib import Path

import production_goat_batch_ledger as ledger
import production_paid_request_journal as journal
import benchmark_longitudinal_g7d_accounting_policy as tariff
from benchmark_longitudinal_provider import (
    build_provider_request_identity, normalize_provider_response,
)
from command_code_provider_transport import accept_command_code_response
from production_goat_auxiliary_policy import completion_exposure
from production_goat_cap_recovery import accepted_envelope
from production_provider_runtime import provider_identity


REVISION = "goat-batch-journal-evidence-b2b1-v1"
# Recovery identities need additional proofs, even when their base hash is standard.
ORDINARY = frozenset({
    "chapter_translation", "state_extraction", "state_json_repair",
    "strict_terminology_micro_repair", "strict_terminology_micro_repair_correction",
    "residual_cjk_micro_repair", "residual_cjk_micro_repair_correction",
    "glossary_candidate_conflict_adjudication", "generic_json_repair",
    "chapter_glossary_review", "chapter_glossary_review_json_repair",
    "chapter_glossary_review_schema_correction", "chapter_glossary_review_semantic_correction",
    "chapter_glossary_review_semantic_correction_retry", "periodic_audit",
    "periodic_audit_json_repair", "periodic_audit_semantic_correction",
})
CONTEXT = re.compile(r"context_(state_extraction|state_json_repair|glossary_review|periodic_audit|terminology_repair)_([0-9a-f]{64})")


class JournalEvidenceError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise JournalEvidenceError(message)


def read_object(path):
    """Production JSON need not be compact; duplicate/nonfinite fields are invalid."""
    path = ledger.safe_path(path)
    require(path.is_file(), "missing/non-file evidence")
    raw = path.read_bytes()
    def unique(pairs):
        value = {}
        for key, item in pairs:
            require(key not in value, "duplicate JSON field")
            value[key] = item
        return value
    def invalid(_):
        raise JournalEvidenceError("nonfinite JSON number")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique, parse_constant=invalid)
        ledger.canonical(value)  # also rejects overflowed exponent floats
    except (UnicodeError, ValueError, TypeError, OverflowError) as exc:
        raise JournalEvidenceError("invalid evidence JSON") from exc
    require(type(value) is dict, "evidence must be an object")
    return value, raw


def _utc(value):
    require(type(value) is str and value == value.strip(), "UTC timestamp string required")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise JournalEvidenceError("malformed UTC timestamp") from exc
    require(parsed.utcoffset() == timedelta(0), "aware UTC timestamp required")
    return parsed


def _pricing(policy, started):
    _utc(started)
    require(ledger.canonical(policy) == ledger.canonical(provider_identity("command_code_provider", "v41-flash")),
            "current provider policy drift")
    identity = tariff.g7d_accounting_identity()
    require(journal._sha256_json(identity) == tariff.G7D_ACCOUNTING_SHA256
            == policy["accounting_policy_sha256"], "authoritative ratecard drift")
    snapshot = tariff.pricing_snapshot(policy["wire_model"], started)
    model = next(m for m in identity["models"] if m["benchmark_model_id"] == policy["wire_model"])
    rates = model["periods"][snapshot["pricing_period"]]
    for name, key in (("cache_read", "cache_hit_rate_per_million"),
                      ("input", "cache_miss_rate_per_million"), ("output", "output_rate_per_million")):
        rate = Decimal(rates[name])  # authoritative decimal spelling, never float arithmetic
        require(rate.is_finite() and rate >= 0 and Decimal(str(snapshot[key])) == rate,
                "pricing snapshot/ratecard mismatch")
    require(snapshot["pricing_version"] == policy["pricing_version"]
            and snapshot["pricing_unit"] == policy["pricing_unit"], "pricing policy drift")
    return dict(snapshot=snapshot, decimal_rates=rates, accounting_policy_sha256=tariff.G7D_ACCOUNTING_SHA256)


def _nano(rates, *, fresh, cached, output):
    for n in (fresh, cached, output):
        ledger.integer(n)
    with localcontext() as ctx:
        # Even an anomalously large durable count must not lose low digits.
        # Bit length exceeds decimal digit count; the margin covers tariff scale.
        ctx.prec = max(100, max(n.bit_length() for n in (fresh, cached, output)) + 100)
        cost = (fresh * Decimal(rates["input"]) + cached * Decimal(rates["cache_read"])
                + output * Decimal(rates["output"])) * Decimal(1000)
        return int(cost.to_integral_value(rounding=ROUND_CEILING))


def _standard_identity(identity, wire):
    kind = identity.get("operation_kind")
    match = CONTEXT.fullmatch(kind) if isinstance(kind, str) else None
    require(kind in ORDINARY or match is not None, "unsupported/special operation identity")
    policy = identity["provider_policy"]
    require(wire.get("model") == policy["wire_model"] and wire.get("reasoning_effort") == "low",
            "wire model/effort drift")
    expected_controls = {"model", "messages", "reasoning_effort"}
    if kind == "chapter_translation":
        expected_controls.add("max_completion_tokens")
        require(type(wire.get("max_completion_tokens")) is int
                and wire["max_completion_tokens"] == policy["translation_completion_tokens"], "translation ceiling drift")
    require(set(wire) == expected_controls, "unsupported physical wire controls")
    messages = wire["messages"]
    require(type(messages) is list and messages and all(type(m) is dict
            and set(m) == {"role", "content"} and m["role"] in {"system", "user", "assistant"}
            and type(m["content"]) is str for m in messages), "unsupported wire messages")
    transport = build_provider_request_identity(request_body=wire)
    require(not match or match[2] == transport.request_sha256, "context wire slot drift")
    expected = journal.request_identity(operation_kind=kind, novel_id=identity["novel_id"],
        chapter=identity["chapter"], provider=policy["provider"], model=policy["wire_model"],
        profile_id=identity["profile_id"], source_sha256=identity["source_sha256"],
        translation_sha256=identity["translation_sha256"],
        request_payload={"wire_request": wire, "provider_policy": policy})
    expected.update(provider_policy=copy.deepcopy(policy), wire_request_sha256=transport.request_sha256)
    # The domain-owned proof is identity data, not permission to recover/send.
    if "auxiliary_domain_binding" in identity:
        expected["auxiliary_domain_binding"] = copy.deepcopy(identity["auxiliary_domain_binding"])
    require(ledger.canonical(expected) == ledger.canonical(identity), "standard journal identity drift")
    ledger.sha(identity["source_sha256"])
    return transport


def exposure_evidence(*, batch_id, journal_path, identity, wire, request_started_utc):
    """Pure proof construction, NOT persistence, reservation or authorization.

    B2B2 must durably publish and independently anchor this exact context before
    reserve/attempt/STARTED. B2B1 only reads fixture-created instances of it.
    """
    _standard_identity(identity, wire)
    pricing = _pricing(identity["provider_policy"], request_started_utc)
    prompt_upper = len(json.dumps(wire, ensure_ascii=False).encode("utf-8")) + 8192
    exposure = completion_exposure(wire, prompt_upper=prompt_upper)
    nano = _nano(pricing["decimal_rates"], fresh=exposure["prompt_upper"], cached=0,
                 output=exposure["completion_upper"])
    return dict(revision=REVISION, batch_id=batch_id, journal_path=str(ledger.safe_path(journal_path)),
        journal_identity=copy.deepcopy(identity), wire_request=copy.deepcopy(wire),
        request_started_utc=request_started_utc, pricing=pricing, completion_exposure=exposure, nano_usd=nano)


class BatchJournalEvidence:
    """Read-only adapter for an existing original B2A ledger, under caller's lock.

    No lock reacquisition or mutation. Missing independent rollback anchoring
    always forbids sends. This class deliberately has no authorize/send method.
    """
    def __init__(self, batch_ledger):
        require(type(batch_ledger) is ledger.BatchLedger, "existing BatchLedger required")
        self.ledger = batch_ledger
        self.root = batch_ledger.root
        self.context_dir = ledger.safe_path(self.root / "data" / "goat_batch_evidence" / batch_ledger.identity["batch_id"])

    def _original_project(self):
        from novel_project import load_novel_project
        from controlled_trial_plan import _immutable_inputs
        project = load_novel_project(self.ledger.binding["novel_id"], repo_root=self.root.parent.parent)
        require(project.root.resolve() == self.root, "inventory project path drift")
        for path in (project.config_file, project.chapters_file, project.translation_addendum_file,
                     project.translation_prompt_file, project.source_overrides_file):
            ledger.safe_path(path)
        require(ledger.canonical(_immutable_inputs(project)) == ledger.canonical(self.ledger.binding["immutable_inputs"]),
                "original source/configuration inputs drift")
        return project

    def _bound_context(self, path, request):
        snapshot = self.ledger.snapshot_locked()  # rehash original checkout and whole ledger
        record = snapshot["operations"].get(request["operation_id"])
        require(record is not None and ledger.canonical(record["request"]) == ledger.canonical(request),
                "request is not an exact retained reservation")
        self.ledger._request(request)
        path = ledger.safe_path(path)
        identity = request["journal_identity"]
        expected_path = journal.paid_request_journal_path(self.root / "data" / "provider_request_journal",
                                                        identity["operation_kind"], request["chapter"])
        require(str(path) == request["journal_path"] == str(expected_path), "journal slot/path drift")
        require(identity["profile_id"] == self.ledger.binding["profile"], "profile drift")
        project = self._original_project()
        from chapter_library import index_chapter_library
        sources = index_chapter_library(json.loads(project.chapters_file.read_text(encoding="utf-8")),
            project.config.final_chapter, project.config.chapter_numbering)
        require(hashlib.sha256(sources[request["chapter"]]["text"].encode("utf-8")).hexdigest()
                == identity["source_sha256"], "journal source provenance drift")
        context, _ = read_object(self.context_dir / (request["operation_id"] + ".json"))
        require(ledger.digest(context) == request["exposure"]["evidence_sha256"], "exposure context hash drift")
        expected = exposure_evidence(batch_id=request["batch_id"], journal_path=path, identity=identity,
            wire=context["wire_request"], request_started_utc=context["request_started_utc"])
        require(ledger.canonical(context) == ledger.canonical(expected)
                and context["nano_usd"] == request["exposure"]["nano_usd"], "exposure proof drift")
        return context

    def _load(self, path, request):
        context = self._bound_context(path, request)
        if not Path(path).exists():
            return context, None
        strict, raw = read_object(path)
        record = journal.load_matching_journal(path, request["journal_identity"])
        require(record is not None and ledger.canonical(strict) == ledger.canonical(record)
                and Path(path).read_bytes() == raw, "journal changed during validation")
        fixed = {"schema_version", "status", "attempt_context", "response", "response_sha256",
                 "error", "created_utc", "updated_utc", "record_sha256"}
        require(set(record) == fixed | set(request["journal_identity"])
                and type(record["schema_version"]) is int, "unknown journal fields/schema")
        _utc(record["created_utc"])
        _utc(record["updated_utc"])
        if record["status"] == "planned":
            require(record["attempt_context"] == {} and record["error"] == {}, "PLANNED evidence drift")
        else:
            require(record["attempt_context"] == {"request_started_utc": context["request_started_utc"]},
                    "request-start timestamp/context drift")
        return context, record

    def validate_observation_locked(self, path, request):
        _, record = self._load(path, request)
        return dict(status=record["status"].upper() if record else "ABSENT",
            reservation_sha256=ledger.digest(request), evidence_sha256=ledger.digest(record))

    def validate_recorded_response_locked(self, path, request):
        context, record = self._load(path, request)
        require(record is not None and record["status"] == "response_recorded", "no recorded response")
        require(record["error"] == {}, "recorded response contains error")
        response = record["response"]
        require(response.get("request_started_utc") == context["request_started_utc"], "response timestamp drift")
        raw = response.get("raw_response")
        require(type(raw) is dict and set(raw) == {"id", "model", "choices", "usage"}, "unsupported raw response")
        require(type(raw["id"]) is str and bool(raw["id"].strip()), "missing provider response ID")
        from production_goat_runtime import raw_response_record
        require(ledger.canonical(raw) == ledger.canonical(raw_response_record(raw)), "noncanonical raw response shape")
        transport = build_provider_request_identity(request_body=context["wire_request"])
        accepted = accept_command_code_response(transport, normalize_provider_response(raw, request_identity=transport))
        usage = accepted.usage
        if usage.total_tokens is not None:
            require(usage.total_tokens == usage.prompt_tokens + usage.completion_tokens, "inconsistent total tokens")
        if usage.reasoning_tokens is not None:
            require(usage.reasoning_tokens <= usage.completion_tokens, "inconsistent reasoning tokens")
        core = dict(raw_response=raw, request_started_utc=context["request_started_utc"])
        envelope = accepted_envelope(core, context["wire_request"])
        require(set(response) <= set(envelope), "unknown response enrichment fields")
        legacy_stats = {k: v for k, v in envelope["stats"].items() if k not in
                        {"provider", "reasoning_tokens", "reasoning_tokens_exposed", "visible_completion_tokens"}}
        for key, value in response.items():
            choices = (envelope[key], legacy_stats) if key == "stats" else (envelope[key],)
            require(any(ledger.canonical(value) == ledger.canonical(expected) for expected in choices),
                    "divergent response enrichment: " + key)
        cached = usage.cached_tokens if usage.cached_tokens_exposed else 0
        nano = _nano(context["pricing"]["decimal_rates"], fresh=usage.prompt_tokens - cached,
                     cached=cached, output=usage.completion_tokens)
        # Physical hashes were verified above. Receipt binds a stable virtual
        # record projection so legitimate later enrichment cannot double-charge.
        immutable = {k: v for k, v in record.items() if k not in
                     {"record_sha256", "updated_utc", "response", "response_sha256"}}
        immutable.update(response=core, response_sha256=journal._sha256_json(core))
        return dict(status="RESPONSE_RECORDED", reservation_sha256=ledger.digest(request),
            journal_record_sha256=journal._sha256_json(dict(revision=REVISION, immutable_record=immutable)),
            response_sha256=journal._sha256_json(core), usage_sha256=journal._sha256_json(raw["usage"]),
            actual=dict(unit=ledger.UNIT, nano_usd=nano))

    def inspect_locked(self):
        """Both-direction inventory. Always NO_SEND, including a valid response."""
        snapshot = self.ledger.snapshot_locked()
        root = ledger.safe_path(self.root / "data" / "provider_request_journal")
        paths = list(root.iterdir()) if root.exists() else []
        require(not root.exists() or root.is_dir(), "invalid journal root")
        operations = snapshot["operations"]
        expected = {r["request"]["journal_path"]: r["request"] for r in operations.values()}
        contexts = list(self.context_dir.iterdir()) if self.context_dir.exists() else []
        require({p.name for p in contexts} == {op + ".json" for op in operations},
                "missing/foreign/orphan context evidence")
        initial = self.ledger.binding["initial_evidence"]
        historical = {k: v for k, v in initial.items()
                      if k.startswith("data/provider_request_journal/") and v is not None}
        seen, observed_history, observations, receipts = set(), set(), {}, {}
        for path in paths:
            record, raw = read_object(path)
            require(path.suffix == ".json", "foreign/temporary journal evidence")
            value = journal.load_matching_journal(path, {})
            require(ledger.canonical(record) == ledger.canonical(value), "journal changed during inventory")
            op = record.get("operation_id")
            ledger.sha(op)
            require(op not in seen, "duplicate journal operation ID")
            seen.add(op)
            if str(path) in expected:
                require(op == expected[str(path)]["operation_id"], "operation/path conflict")
                continue
            key = path.relative_to(self.root).as_posix()
            require(key in historical and hashlib.sha256(raw).hexdigest() == historical[key]
                    and type(record.get("chapter")) is int
                    and record["chapter"] < self.ledger.binding["initial_chapter"]
                    and record.get("novel_id") == self.ledger.binding["novel_id"]
                    and record.get("profile_id") == self.ledger.binding["profile"]
                    and record["status"] in {"response_recorded", "uncertain"}, "journal-ahead/unproved historical journal")
            observed_history.add(key)
        require(observed_history == set(historical), "missing original historical journal")
        # Preserve existing historical dispositions and audit/lineage obligations.
        from production_uncertain_request_disposition import verified_completed_dispositions
        project = self._original_project()
        inventory = journal.inspect_paid_request_journals(root)
        effective = verified_completed_dispositions(project, inventory)
        require(not effective["errors"], "invalid effective journal inventory")
        unresolved = {r["operation_id"] for r in effective["uncertain"]}
        for request in expected.values():
            path, op = Path(request["journal_path"]), request["operation_id"]
            observation = self.validate_observation_locked(path, request)
            observations[op] = observation
            if observation["status"] == "RESPONSE_RECORDED":
                receipts[op] = self.validate_recorded_response_locked(path, request)
                retained = operations[op]["receipt"]
                require(retained is None or ledger.canonical(retained) == ledger.canonical(receipts[op]),
                        "settled receipt evidence drift")
            else:
                require(operations[op]["status"] != "SETTLED", "settled evidence regressed")
        require(not (unresolved - set(operations)), "unresolved historical paid request")
        return dict(paid_execution_authorized=False, send_authorized=False,
            execution_blocker="B2B2_TRUSTED_HOOK_AND_INDEPENDENT_ANCHOR_REQUIRED",
            observations=observations, receipts=receipts, historical_files=sorted(observed_history),
            ambiguous_operations=sorted(op for op, observation in observations.items()
                if observation["status"] in {"STARTED", "UNCERTAIN"} or operations[op]["status"] == "ATTEMPTED"
                and observation["status"] != "RESPONSE_RECORDED"))
