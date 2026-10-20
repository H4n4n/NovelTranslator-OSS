"""Immutable request policy for the active production engine; no transport or IO.

Model routing/pricing remains in translation_model_config. State extraction owns
its specialized budgets and recovery protocol. Domain helpers own eligibility,
durable attempt counters and validation; these definitions describe their limits,
not permission to send or a replacement executor.
"""
from dataclasses import asdict, dataclass, field
import hashlib
import json

from production_state_extraction import DEFAULT_STATE_POLICY, StateExtractionPolicy
from translation_model_config import resolve_translation_model

POLICY_SCHEMA_VERSION = 1
POLICY_REVISION = "production-operation-policy-v2"


@dataclass(frozen=True)
class OperationRequestPolicy:
    operation_id: str
    max_tokens: int
    thinking_type: str = "disabled"
    reasoning_effort: str | None = None
    mode: str = "normal"
    parent_operation: str | None = None
    max_attempts: int = 1
    trigger: str | None = None

    def __post_init__(self):
        if not isinstance(self.operation_id, str) or not self.operation_id.strip():
            raise ValueError("operation identifier must be nonempty")
        if type(self.max_tokens) is not int or self.max_tokens < 1:
            raise ValueError("output ceiling must be a positive integer")
        if self.thinking_type not in {"disabled", "enabled"}:
            raise ValueError("unsupported thinking type")
        if self.reasoning_effort not in {None, "low"}:
            raise ValueError("unsupported reasoning effort; HIGH is not authorized")
        if self.thinking_type == "disabled" and self.reasoning_effort is not None:
            raise ValueError("disabled thinking cannot have reasoning effort")
        if self.mode not in {"normal", "recovery", "repair", "correction"}:
            raise ValueError("unsupported operation mode")
        if type(self.max_attempts) is not int or self.max_attempts != 1:
            raise ValueError("each logical operation authorizes at most one attempt")
        for name, value in (("parent_operation", self.parent_operation), ("trigger", self.trigger)):
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{name} must be a nonempty immutable string")
        if self.mode == "normal":
            if self.parent_operation is not None or self.trigger is not None:
                raise ValueError("normal operations cannot declare a followup trigger")
        elif not self.parent_operation or not self.trigger:
            raise ValueError("followup operations require a parent and bounded trigger")

    def request_controls(self):
        # Return fresh JSON containers; mutation cannot modify policy identity.
        controls = {"max_tokens": self.max_tokens,
                    "extra_body": {"thinking": {"type": self.thinking_type}}}
        if self.reasoning_effort is not None:
            controls["reasoning_effort"] = self.reasoning_effort
        return controls

    def identity(self):
        return asdict(self)

    def event_fields(self):
        return {"operation_kind": self.operation_id, "policy_revision": POLICY_REVISION,
                "completion_ceiling": self.max_tokens, "thinking_type": self.thinking_type,
                "reasoning_effort": self.reasoning_effort, "policy_mode": self.mode}


# Each followup is a distinct logical operation. In particular schema correction
# and semantic correction share the chapter-review budget; only an allowlisted
# ineffective semantic patch can reach the existing second semantic correction.
# Those guards stay authoritative in chapter_glossary_review, not this catalog.
DEFAULT_AUXILIARY_POLICIES = (
    OperationRequestPolicy("chapter_glossary_review", 4000),
    OperationRequestPolicy("chapter_glossary_review_json_repair", 5000, mode="repair",
                           parent_operation="chapter_glossary_review", trigger="malformed_json"),
    OperationRequestPolicy("chapter_glossary_review_schema_correction", 2000, mode="correction",
                           parent_operation="chapter_glossary_review", trigger="eligible_model_schema_error"),
    OperationRequestPolicy("chapter_glossary_review_semantic_correction", 2000, mode="correction",
                           parent_operation="chapter_glossary_review", trigger="eligible_semantic_error"),
    OperationRequestPolicy("chapter_glossary_review_semantic_correction_retry", 2000, mode="correction",
                           parent_operation="chapter_glossary_review_semantic_correction",
                           trigger="eligible_ineffective_semantic_patch"),
    OperationRequestPolicy("periodic_audit", 4000),
    OperationRequestPolicy("periodic_audit_json_repair", 4000, mode="repair",
                           parent_operation="periodic_audit", trigger="malformed_nonlength_chunk"),
    OperationRequestPolicy("periodic_audit_semantic_correction", 4000, mode="correction",
                           parent_operation="periodic_audit", trigger="eligible_semantic_field_error"),
    OperationRequestPolicy("residual_cjk_micro_repair", 800),
    OperationRequestPolicy("residual_cjk_micro_repair_correction", 800, mode="correction",
                           parent_operation="residual_cjk_micro_repair", trigger="eligible_patch_error"),
    OperationRequestPolicy("strict_terminology_micro_repair", 800),
    OperationRequestPolicy("strict_terminology_micro_repair_correction", 800, mode="correction",
                           parent_operation="strict_terminology_micro_repair", trigger="eligible_patch_error"),
    OperationRequestPolicy("glossary_candidate_conflict_adjudication", 800),
    OperationRequestPolicy("generic_json_repair", 5000),
)


@dataclass(frozen=True)
class ProductionOperationPolicy:
    translation: OperationRequestPolicy
    state: StateExtractionPolicy = DEFAULT_STATE_POLICY
    translation_recovery: OperationRequestPolicy | None = None
    auxiliary: tuple[OperationRequestPolicy, ...] = DEFAULT_AUXILIARY_POLICIES
    model_id: str = ""
    model_version: str = ""
    revision: str = field(default=POLICY_REVISION, init=False)

    def __post_init__(self):
        if not isinstance(self.model_id, str) or not isinstance(self.model_version, str):
            raise ValueError("model binding must contain immutable strings")
        if not isinstance(self.translation, OperationRequestPolicy) or (
            self.translation.operation_id != "chapter_translation" or self.translation.mode != "normal"
        ):
            raise ValueError("translation must be the normal chapter_translation operation")
        if self.translation_recovery is not None:
            recovery = self.translation_recovery
            if not isinstance(recovery, OperationRequestPolicy) or (
                recovery.operation_id != "chapter_translation_recovery"
                or recovery.mode != "recovery"
                or recovery.parent_operation != "chapter_translation"
                or recovery.max_tokens <= self.translation.max_tokens
            ):
                raise ValueError("translation recovery must be a bounded escalation")
        if not isinstance(self.state, StateExtractionPolicy):
            raise ValueError("state policy must be StateExtractionPolicy")
        if type(self.auxiliary) is not tuple or any(
            not isinstance(item, OperationRequestPolicy) for item in self.auxiliary
        ):
            raise ValueError("auxiliary policies must be an immutable tuple")
        expected = {item.operation_id for item in DEFAULT_AUXILIARY_POLICIES}
        observed = [item.operation_id for item in self.auxiliary]
        if len(observed) != len(expected) or set(observed) != expected:
            raise ValueError("auxiliary operation roster is incomplete or duplicated")
        operations = self.operations()
        by_id = {item.operation_id: item for item in operations}
        for item in operations:
            seen = {item.operation_id}
            parent = item.parent_operation
            while parent is not None:
                if parent not in by_id or parent in seen:
                    raise ValueError("invalid or cyclic operation followup")
                seen.add(parent)
                parent = by_id[parent].parent_operation

    def operations(self):
        state = self.state
        recovery = () if self.translation_recovery is None else (self.translation_recovery,)
        return (self.translation, *recovery,
                OperationRequestPolicy("state_extraction", state.initial_tokens,
                                       state.thinking_type, state.reasoning_effort),
                OperationRequestPolicy("state_extraction_recovery", state.recovery_tokens,
                                       state.thinking_type, state.reasoning_effort, "recovery",
                                       "state_extraction", trigger="length_or_blank_stop"),
                OperationRequestPolicy("state_json_repair", state.repair_tokens,
                                       state.thinking_type, state.reasoning_effort, "repair",
                                       "state_extraction", trigger="substantive_malformed_state"),
                *self.auxiliary)

    def for_operation(self, operation_id):
        for policy in self.operations():
            if policy.operation_id == operation_id:
                return policy
        raise ValueError(f"unknown production operation: {operation_id!r}")

    def identity(self):
        return {"schema_version": POLICY_SCHEMA_VERSION, "revision": self.revision,
                "model_binding": {"model": self.model_id, "version": self.model_version},
                "translation_recovery": (None if self.translation_recovery is None
                                         else self.translation_recovery.identity()),
                "state_extraction_policy": self.state.identity(),
                "operations": [item.identity() for item in self.operations()]}

    def identity_sha256(self):
        return hashlib.sha256(json.dumps(self.identity(), sort_keys=True,
                                        separators=(",", ":")).encode("utf-8")).hexdigest()


def policy_for_model(selection=None, *, state=DEFAULT_STATE_POLICY):
    """Snapshot model request controls, never pricing or route authorization.

    Describing a retired route for historical replay is allowed. Only the
    existing model identity runtime can authorize a new production send.
    """
    config = resolve_translation_model(selection)
    return ProductionOperationPolicy(
        translation=OperationRequestPolicy("chapter_translation", config.max_tokens, config.thinking),
        state=state, model_id=config.model, model_version=config.version,
        translation_recovery=(OperationRequestPolicy(
            "chapter_translation_recovery", 24000, mode="recovery",
            parent_operation="chapter_translation", trigger="durable_length_response",
        ) if config.model == "deepseek-flash" else None),
    )
