"""Production-owned auxiliary intent roster and fail-closed credit exposure.

The reviewed physical context bound is source-bound to a local immutable
evidence artifact. It authorizes financial exposure, never a wire output cap.
No network, credentials or IO occurs at import; every authorization rehashes
and validates the evidence before STARTED. A separate output bound is unknown.
"""
from dataclasses import dataclass, asdict
import hashlib
import json
from pathlib import Path
from types import MappingProxyType
import re

from production_operation_policy import policy_for_model

@dataclass(frozen=True, slots=True)
class ReviewedCapability:
    """Compiled, immutable provider capability and exact reviewed provenance."""
    provider: str
    model: str
    context_tokens: int
    output_tokens: int | None
    evidence_path: str
    evidence_sha256: str
    revision: str
    schema_version: int = 1
    primary_source: str = "https://commandcode.ai/models/deepseek-v4-1-flash"
    primary_source_reviewed_fact: str = "context window = 1M tokens"
    secondary_source: str = "https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash"
    secondary_source_reviewed_fact: str = "model supports contexts up to one million tokens"
    reviewed_date: str = "2026-10-11"
    normalization_explanation: str = "1M / one million is normalized to decimal 1,000,000 tokens."

    def __post_init__(self):
        strings = (self.provider, self.model, self.evidence_path, self.evidence_sha256,
            self.revision, self.primary_source, self.primary_source_reviewed_fact,
            self.secondary_source, self.secondary_source_reviewed_fact,
            self.reviewed_date, self.normalization_explanation)
        if any(not isinstance(value, str) or not value.strip() for value in strings):
            raise ValueError("reviewed capability provenance must be immutable nonempty strings")
        if (type(self.context_tokens) is not int or self.context_tokens < 1
            or (self.output_tokens is not None and (type(self.output_tokens) is not int or self.output_tokens < 1))):
            raise ValueError("reviewed capability bounds must be positive integers or unknown output")

    def expected_evidence(self):
        value = asdict(self)
        value["normalized_context_tokens"] = value.pop("context_tokens")
        value.pop("evidence_path")
        value.pop("evidence_sha256")
        return value

    def validate_evidence(self, *, provider, model):
        if (self.provider != provider or self.model != model
            or type(self.context_tokens) is not int or self.context_tokens < 1
            or (self.output_tokens is not None and (type(self.output_tokens) is not int or self.output_tokens < 1))
            or type(self.schema_version) is not int or self.schema_version != 1
            or not isinstance(self.evidence_path, str) or Path(self.evidence_path).suffix != ".json"
            or not isinstance(self.evidence_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", self.evidence_sha256)):
            raise ValueError("compiled reviewed capability identity is invalid")
        path = Path(__file__).resolve().parent / self.evidence_path
        try:
            if path.is_symlink():
                raise ValueError("reviewed capability evidence cannot be a symlink")
            raw = path.read_bytes()
        except OSError as exc:
            raise ValueError("reviewed capability evidence is unavailable") from exc
        # Never cache: rehash the exact on-disk bytes before every authorization.
        if hashlib.sha256(raw).hexdigest() != self.evidence_sha256:
            raise ValueError("reviewed capability evidence SHA mismatch")
        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate reviewed evidence field")
                result[key] = value
            return result
        try:
            evidence = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object)
            # Canonical comparison also distinguishes booleans/floats from the
            # compiled integer schema/context and rejects unknown fields.
            canonical = lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), allow_nan=False)
            if canonical(evidence) != canonical(self.expected_evidence()):
                raise ValueError("reviewed capability facts/schema/provenance drift")
        except (UnicodeDecodeError, TypeError, ValueError) as exc:
            raise ValueError("reviewed capability evidence does not match compiled policy") from exc
        return asdict(self)


# Reviewed provider/model capability sources only. NOT runtime configurable.
# The specific evidence path is pinned to LF in .gitattributes for exact-byte
# SHA stability across Windows/CI clones. No authoritative output bound is claimed.
COMMAND_CODE_FLASH_CAPABILITY = ReviewedCapability(
    provider="command_code_provider", model="deepseek/deepseek-v4.1-flash",
    context_tokens=1_000_000, output_tokens=None,
    evidence_path="docs/provider_capabilities/command_code_deepseek_v4_1_flash_2026_10_11.json",
    evidence_sha256="99138eabe21ec68eda4fb18751b92bec38508924e5830a36c77317ba64ef3c99",
    revision="command-code-deepseek-v41-flash-context-capability-v1")
REVIEWED_HARD_BOUNDS = MappingProxyType({
    (COMMAND_CODE_FLASH_CAPABILITY.provider, COMMAND_CODE_FLASH_CAPABILITY.model): COMMAND_CODE_FLASH_CAPABILITY})


def domain_roster():
    from production_goat_runtime import DOMAIN_OPERATIONS
    logical = {**DOMAIN_OPERATIONS, "state_extraction": "state_extraction",
        "state_json_repair": "state_json_repair", "generic_json_repair": "state_json_repair"}
    catalog = policy_for_model("v41-flash")
    ordinary = tuple({"operation": item.operation_id, "logical": logical[item.operation_id],
        "domain_tokens": item.max_tokens, "max_attempts": item.max_attempts,
        "mode": item.mode, "parent": item.parent_operation}
        for item in catalog.operations() if item.operation_id not in
        {"chapter_translation", "chapter_translation_recovery"})
    # GOAT-only manual operation; keep the direct operation catalog unchanged.
    from production_goat_state_terminal_recovery import KIND
    return ordinary + ({"operation": KIND, "logical": "state_extraction", "domain_tokens": 4000,
        "max_attempts": 1, "mode": "recovery", "parent": "state_extraction"},)


def domain_intents(logical):
    values = {item["domain_tokens"] for item in domain_roster() if item["logical"] == logical}
    # Explicit reconstruction of the legacy domain 3K state input remains
    # possible; journal identity still refuses old-policy replay as new policy.
    if logical in {"state_extraction", "state_json_repair"}:
        values.add(3000)
    if not values:
        raise ValueError("unknown production GOAT auxiliary operation")
    return values


def completion_exposure(request, *, prompt_upper):
    """Conservative accounting bound, never inserted into the physical wire.

    A context-only contract bounds TOTAL tokens; reserving the whole context
    for each of input and output is deliberately conservative. Subtracting an
    estimated prompt upper from context would
    UNDER-reserve completion and is forbidden. Reviewed output bounds can be
    used independently. No arbitrary sentinel or hidden send ceiling exists.
    """
    if type(prompt_upper) is not int or prompt_upper < 1:
        raise ValueError("invalid conservative prompt estimate")
    if "max_tokens" in request:
        raise ValueError("legacy cap must not reach authorization")
    if "max_completion_tokens" in request:
        ceiling = request["max_completion_tokens"]
        if type(ceiling) is not int or ceiling < 1:
            raise ValueError("invalid explicit completion ceiling")
        return dict(revision="explicit-completion-exposure-v1", prompt_upper=prompt_upper,
            completion_upper=ceiling, capability_source=None)
    if (request.get("model") != "deepseek/deepseek-v4.1-flash"
        or request.get("reasoning_effort") != "low"):
        raise ValueError("unbounded authorization provider/model/effort drift")
    bound = REVIEWED_HARD_BOUNDS.get(("command_code_provider", request["model"]))
    if not isinstance(bound, ReviewedCapability):
        raise ValueError("no repository-reviewed authoritative provider hard bound")
    evidence = bound.validate_evidence(provider="command_code_provider", model=request["model"])
    context = bound.context_tokens
    output = bound.output_tokens
    if context is not None and (type(context) is not int or context < prompt_upper):
        raise ValueError("request outside conservative context envelope")
    if output is not None and (type(output) is not int or output < 1):
        raise ValueError("invalid authoritative output bound")
    if context is None and output is None:
        raise ValueError("missing authoritative context/output bound")
    # With context-only metadata the actual prompt can be smaller than the
    # estimate. The whole context is a conservative completion upper bound.
    return dict(revision="provider-hard-bound-exposure-v1", prompt_upper=context or prompt_upper,
        completion_upper=min(context, output) if context and output else context or output,
        capability_source=evidence)
