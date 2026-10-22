"""Bounded state extraction, independent of transport and novel filesystem state."""
from copy import deepcopy
from dataclasses import asdict, dataclass
import json
import re

from production_failure_classification import ProviderResponseError


@dataclass(frozen=True)
class StateExtractionPolicy:
    initial_tokens: int = 4000
    recovery_tokens: int = 8000
    repair_tokens: int = 5000
    thinking_type: str = "disabled"
    reasoning_effort: str | None = None

    def __post_init__(self):
        for value in (self.initial_tokens, self.recovery_tokens, self.repair_tokens):
            if type(value) is not int or not 1 <= value <= 12000:
                raise ValueError("state budgets must be integers between 1 and 12000")
        if self.recovery_tokens <= self.initial_tokens:
            raise ValueError("state recovery must have a larger bounded ceiling")
        if self.thinking_type not in {"disabled", "enabled"}:
            raise ValueError("unsupported state thinking strategy")
        if self.reasoning_effort not in {None, "low"}:
            raise ValueError("ordinary state extraction must not use HIGH reasoning")
        if self.thinking_type == "disabled" and self.reasoning_effort is not None:
            raise ValueError("disabled thinking cannot have a reasoning effort")

    def request_controls(self, ceiling):
        controls = {"max_tokens": ceiling,
                    "extra_body": {"thinking": {"type": self.thinking_type}}}
        if self.reasoning_effort is not None:
            controls["reasoning_effort"] = self.reasoning_effort
        return controls

    def identity(self):
        return {"revision": "bounded-state-extraction-v1", **asdict(self)}


# Direct production keeps its existing verified request controls. A reasoning-
# enabled adapter owns its exact verified LOW parameter serialization; it must
# retain these ceilings rather than reverting to the historical 3000 allowance.
DEFAULT_STATE_POLICY = StateExtractionPolicy()


class StateExtractionError(ProviderResponseError):
    def __init__(self, category):
        self.category = category
        super().__init__(f"State extraction blocked: {category}. Preserve paid pending "
                         "translation and inspect the durable state journals; automatic "
                         "additional attempts are forbidden.")


def classify_state_response(envelope, clean_text):
    content = envelope.get("content")
    if not isinstance(content, str):
        raise StateExtractionError("invalid_visible_content_type")
    if envelope.get("finish_reason") == "length":
        return "completion_budget_exhausted"
    if envelope.get("finish_reason") != "stop":
        raise StateExtractionError("unexpected_finish_reason")
    if not clean_text(content).strip():
        return "blank_visible_content"
    return "visible_state"


def validate_state(state):
    """Require usable continuity; candidate semantics stay with candidate preparation.

    No glossary candidates is normal. Exact source identity, categories and
    duplicate proposal conflicts remain owned by the established source-backed
    candidate preparation/review path, rather than a competing validator here.
    """
    if not isinstance(state, dict) or not {"new_terms", "continuity_summary", "issues"} <= state.keys():
        raise StateExtractionError("invalid_state_schema")
    if not isinstance(state["new_terms"], list) or not isinstance(state["issues"], list):
        raise StateExtractionError("invalid_state_schema")
    if not isinstance(state["continuity_summary"], str):
        raise StateExtractionError("invalid_state_schema")
    if not state["continuity_summary"].strip():
        raise StateExtractionError("empty_continuity")
    if any(not isinstance(term, dict) for term in state["new_terms"]):
        raise StateExtractionError("invalid_state_candidate")
    return state


def has_repairable_state(content):
    # Require an emitted, nonempty state field, not a JSON shell or arbitrary prose.
    return bool(re.search(r'"(?:continuity_summary|english)"\s*:\s*"[^"\s]', content))


def _run_state_extraction(*, request, repair, clean_text, salvage, combine_stats,
                         emit, policy=DEFAULT_STATE_POLICY):
    """Initial -> at most one extraction recovery -> local salvage/one repair.

    request/repair MUST be durable journal callbacks. Uncertain outcomes and local
    persistence errors propagate; they never trigger this response-only recovery.
    """
    stats = None
    for operation, ceiling in (("state_extraction", policy.initial_tokens),
                               ("state_extraction_recovery", policy.recovery_tokens)):
        journaled = request(operation, ceiling)
        envelope = journaled["response"]
        stats = envelope["stats"] if stats is None else combine_stats(stats, envelope["stats"])
        category = classify_state_response(envelope, clean_text)
        emit("state_extraction_attempt", operation=operation, completion_ceiling=journaled.get("request_ceiling", ceiling),
             wire_completion_policy=journaled.get("wire_completion_policy"),
             thinking_type=policy.thinking_type, reasoning_effort=policy.reasoning_effort,
             finish_reason=envelope.get("finish_reason"),
             visible_characters=len(envelope["content"].strip()), classification=category,
             replayed_paid_response=journaled["replayed"],
             paid_operation_id=journaled["operation_id"], token_usage=envelope["stats"])
        if category == "visible_state":
            break
        if operation == "state_extraction_recovery":
            raise StateExtractionError("recovery_" + category)
        emit("state_extraction_recovery_triggered", failure_category=category,
             recovery_ceiling=policy.recovery_tokens, health_status="degraded")
    return parse_state_response(envelope=envelope, stats=stats, repair=repair,
        clean_text=clean_text, salvage=salvage, combine_stats=combine_stats,
        emit=emit, policy=policy, recovery_used=operation == "state_extraction_recovery")


def parse_state_response(*, envelope, stats, repair, clean_text, salvage,
                         combine_stats, emit, policy=DEFAULT_STATE_POLICY, recovery_used=False):
    """Shared STOP-state parsing with local salvage and at most one JSON repair.

    No state-generation callback exists here. Explicit terminal recovery can
    reuse parsing without entering the ordinary generation-recovery loop.
    """
    if classify_state_response(envelope, clean_text) != "visible_state":
        raise StateExtractionError("terminal_generation_unusable")
    content = clean_text(envelope["content"])
    local_salvage = False
    repaired = False
    try:
        state = json.loads(content)
    except json.JSONDecodeError:
        state = salvage(content)
        local_salvage = state is not None
        if state is None:
            if not has_repairable_state(content):
                raise StateExtractionError("no_repairable_state")
            repair_result = repair(content, policy.repair_tokens)
            repair_envelope = repair_result["response"]
            stats = combine_stats(stats, repair_envelope["stats"])
            repair_category = classify_state_response(repair_envelope, clean_text)
            emit("state_json_repair_attempt", completion_ceiling=policy.repair_tokens,
                 classification=repair_category, finish_reason=repair_envelope.get("finish_reason"),
                 paid_operation_id=repair_result["operation_id"],
                 replayed_paid_response=repair_result["replayed"], token_usage=repair_envelope["stats"])
            if repair_category != "visible_state":
                raise StateExtractionError("repair_" + repair_category)
            try:
                state = json.loads(clean_text(repair_envelope["content"]))
            except json.JSONDecodeError as exc:
                raise StateExtractionError("malformed_repair") from exc
            repaired = True
    validate_state(state)
    emit("state_extraction_accepted", local_salvage_used=local_salvage,
         json_repair_used=repaired, recovery_used=recovery_used,
         candidate_count=len(state["new_terms"]), continuity_characters=len(state["continuity_summary"]),
         health_status="healthy", token_usage=stats)
    return state, stats


def run_state_extraction(*, emit, **kwargs):
    # Retain each accepted direct response's actual rate window. The numeric
    # combiner intentionally has no single pricing period for multiple calls.
    # Without components, a saved recovered state fails startup validation or
    # is incorrectly repriced at one rate. Domain journals remain authoritative.
    operations = []
    request, repair = kwargs["request"], kwargs["repair"]

    def observed(kind, callback, *args):
        result = callback(*args)
        operations.append({"operation_kind": kind, "operation_id": result["operation_id"],
                           "stats": deepcopy(result["response"]["stats"])})
        return result

    kwargs["request"] = lambda kind, ceiling: observed(kind, request, kind, ceiling)
    kwargs["repair"] = lambda content, ceiling: observed("state_json_repair", repair, content, ceiling)
    try:
        state, stats = _run_state_extraction(emit=emit, **kwargs)
        if len(operations) > 1 and stats.get("provider") != "command_code_provider":
            stats = {**stats, "state_accepted_operations": operations}
        return state, stats
    except StateExtractionError as exc:
        emit("state_extraction_blocked", failure_category=exc.category,
             health_status="blocked")
        raise
