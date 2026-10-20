"""Immutable experiment policy; no production eligibility or import-time IO."""
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import os

BASE_URL = "https://api.commandcode.ai/provider/v1"
ENDPOINT = "/chat/completions"
PROVIDER = "command_code_provider"
SEMANTIC = frozenset({"chapter_glossary_review", "chapter_glossary_review_semantic_correction",
    "chapter_glossary_review_semantic_correction_retry", "periodic_audit",
    "periodic_audit_semantic_correction", "residual_cjk_micro_repair",
    "residual_cjk_micro_repair_correction", "strict_terminology_micro_repair",
    "strict_terminology_micro_repair_correction", "glossary_candidate_conflict_adjudication"})
STRUCTURED = frozenset({"state_extraction", "state_extraction_recovery", "state_json_repair",
    "generic_json_repair", "chapter_glossary_review_json_repair",
    "chapter_glossary_review_schema_correction", "periodic_audit_json_repair"})
OPERATIONS = SEMANTIC | STRUCTURED | {"chapter_translation"}

class ExperimentError(RuntimeError):
    """Fail closed on unsafe configuration, evidence or execution."""


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def text_sha(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def decimal(value):
    if isinstance(value, (bool, float)):
        raise ExperimentError("money must be an exact decimal string")
    try:
        result = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ExperimentError("invalid decimal") from exc
    if not result.is_finite() or result < 0:
        raise ExperimentError("money must be finite and nonnegative")
    return result


def positive_int(value):
    if type(value) is not int or value < 1:
        raise ExperimentError("expected positive integer")
    return value


def local_path(path):
    path = Path(path).absolute()
    for component in (path, *path.parents):
        if component.is_symlink() or (hasattr(os.path, "isjunction") and os.path.isjunction(component)):
            raise ExperimentError("linked paths are forbidden")
    return path.resolve()


def external_path(path, protected):
    target = local_path(path)
    for root in (Path("D:/NovelTranslator"), *map(Path, protected)):
        root = local_path(root)
        if target == root or root in target.parents or target in root.parents:
            raise ExperimentError("experiment output overlaps protected root")
    return target


@dataclass(frozen=True)
class ModelPolicy:
    logical_id: str
    provider_model: str
    allowed_efforts: tuple
    reasoning_by_operation: tuple
    translation_ceiling: int
    auxiliary_ceiling: int
    context_tokens: int
    input_rate: str
    output_rate: str
    cache_rate: str
    catalog_provenance: str
    reasoning_field: str = "reasoning_effort"
    capability_status: str = "catalog_declared"
    provider: str = PROVIDER
    base_url: str = BASE_URL
    endpoint: str = ENDPOINT
    retries: int = 0
    fallback: None = None
    provider_invoice_claimed: bool = False

    def __post_init__(self):
        if (self.provider != PROVIDER or self.base_url != BASE_URL or self.endpoint != ENDPOINT
            or type(self.retries) is not int or self.retries != 0 or self.fallback is not None
            or self.provider_invoice_claimed is not False or self.capability_status != "catalog_declared"
            or self.reasoning_field != "reasoning_effort"):
            raise ExperimentError("unsupported experimental transport/capability policy")
        if not all(isinstance(x, str) and x.strip() for x in
                   (self.logical_id, self.provider_model, self.catalog_provenance)):
            raise ExperimentError("model and catalog provenance are required")
        if not isinstance(self.allowed_efforts, tuple) or not self.allowed_efforts or len(set(self.allowed_efforts)) != len(self.allowed_efforts):
            raise ExperimentError("invalid allowed efforts")
        if not set(self.allowed_efforts) <= {"low", "medium", "xhigh"}:
            raise ExperimentError("unknown reasoning effort")
        if type(self.reasoning_by_operation) is not tuple or any(type(x) is not tuple or len(x) != 2 for x in self.reasoning_by_operation):
            raise ExperimentError("operation policy must be immutable pairs")
        mapping = dict(self.reasoning_by_operation)
        if set(mapping) != OPERATIONS or len(mapping) != len(self.reasoning_by_operation):
            raise ExperimentError("explicit complete operation policy required")
        if not set(mapping.values()) <= set(self.allowed_efforts):
            raise ExperimentError("operation effort absent from catalog")
        positive_int(self.translation_ceiling)
        positive_int(self.auxiliary_ceiling)
        positive_int(self.context_tokens)
        if self.auxiliary_ceiling > 8000 or self.translation_ceiling >= self.context_tokens:
            raise ExperimentError("unsafe completion/context ceiling")
        for rate in (self.input_rate, self.output_rate, self.cache_rate):
            decimal(rate)
        if decimal(self.cache_rate) > decimal(self.input_rate):
            raise ExperimentError("cache rate exceeds input rate")

    @classmethod
    def from_dict(cls, value):
        value = dict(value)
        value["allowed_efforts"] = tuple(value["allowed_efforts"])
        value["reasoning_by_operation"] = tuple(sorted(value["reasoning_by_operation"].items()))
        return cls(**value)

    def to_dict(self):
        value = asdict(self)
        value["allowed_efforts"] = list(self.allowed_efforts)
        value["reasoning_by_operation"] = dict(self.reasoning_by_operation)
        return value

    @property
    def sha256(self):
        return digest(self.to_dict())

    def effort(self, operation):
        try:
            return dict(self.reasoning_by_operation)[operation]
        except KeyError as exc:
            raise ExperimentError("unknown experimental operation") from exc

    def ceiling(self, operation):
        self.effort(operation)
        return self.translation_ceiling if operation == "chapter_translation" else self.auxiliary_ceiling

    def charge(self, prompt, completion, cached=0):
        return ((prompt-cached)*decimal(self.input_rate) + cached*decimal(self.cache_rate)
                + completion*decimal(self.output_rate)) / Decimal(1000000)


def qwen_catalog_policy():
    """Configuration fixture only. No live capability is claimed."""
    return ModelPolicy("qwen/qwen3.8-27b", "Qwen/Qwen3.8-27B", ("low", "medium", "xhigh"),
        tuple(sorted((op, "low" if op in STRUCTURED else "medium") for op in OPERATIONS)),
        50000, 8000, 262000, "0.40", "3.00", "0.04",
        "operator-observed-command-code-catalog-2026-10-07; not live verified")
