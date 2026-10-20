"""Import-safe production provider selection and provenance policy."""
import copy
import json
import math
import hashlib
import os
from pathlib import Path

from production_provider_contract import (
    PROVIDER_BACKENDS, PROVIDER_COMMAND_CODE, PROVIDER_DIRECT_DEEPSEEK,
    COMMAND_CODE_DEEPSEEK_V41_FLASH, resolve_provider_backend,
)
from command_code_provider_transport import transport_identity
from benchmark_longitudinal_g7b_auxiliary_policy import reasoning_by_operation
from benchmark_longitudinal_g7d_accounting_policy import G7D_ACCOUNTING_SHA256, PRICING_VERSION
from benchmark_longitudinal_g7j_reasoning_budget import (
    G7J_POLICY_SHA256, G7P_STAGE1_RUNTIME_AUXILIARY_POLICY_SHA256,
)
from translation_model_config import resolve_translation_model

PROVIDER_ENV = "NOVELTRANSLATOR_PROVIDER_BACKEND"
REVISION = "production-provider-runtime-v1"
from production_goat_policy import REVISION as GOAT_POLICY_REVISION, policy_sha256 as goat_policy_sha256


from production_provider_errors import ProductionProviderError


def selected_provider(selection=None):
    value = selection if selection is not None else os.environ.get(PROVIDER_ENV, PROVIDER_DIRECT_DEEPSEEK)
    if value not in PROVIDER_BACKENDS:
        raise ProductionProviderError("unknown production provider; no fallback permitted")
    return value


def reject_unattended_goat(selection=None):
    """Initial rollout allows GOAT only through bounded authorization routes."""
    if selected_provider(selection) == PROVIDER_COMMAND_CODE:
        raise ProductionProviderError(
            "GOAT unattended production is disabled during initial rollout; "
            "use controlled_trial_execute or pending_recovery_execute with "
            "fingerprint, approval token and explicit send/credit-equivalent ceilings")


def provider_identity(selection, model):
    provider = selected_provider(selection)
    config = resolve_translation_model(model)
    if provider == PROVIDER_COMMAND_CODE:
        if config.model != "deepseek-flash":
            raise ProductionProviderError("GOAT requires explicit logical v41-flash model selection")
        return {**transport_identity(), "revision": "production-provider-runtime-v3",
                "auxiliary_completion_policy": "provider_unbounded",
                "logical_model": config.model, "model_version": config.version,
                "reasoning_by_operation": dict(reasoning_by_operation(COMMAND_CODE_DEEPSEEK_V41_FLASH)),
                "translation_budget_policy_revision": GOAT_POLICY_REVISION,
                "translation_budget_policy_sha256": goat_policy_sha256(),
                "auxiliary_budget_policy_sha256": goat_policy_sha256(),
                "accounting_policy_sha256": G7D_ACCOUNTING_SHA256,
                "pricing_version": PRICING_VERSION, "provider_invoice_claimed": False,
                "scheduler_mode": "provider_unrestricted"}
    return {**resolve_provider_backend(provider, wire_model=config.model).identity(),
            "revision": REVISION, "logical_model": config.model,
            "model_version": config.version, "pricing_version": config.pricing_version,
            "scheduler_mode": "direct_deepseek_schedule"}


# Complete pre-50K PR146/PR147 snapshot. Deliberately no active policy constants.
# Canonical encoding: UTF-8 JSON, sorted keys, compact separators, ensure_ascii=False.
_HISTORICAL_GOAT_V1_JSON = '''{
"provider":"command_code_provider",
"base_url":"https://api.commandcode.ai/provider/v1",
"chat_completions_path":"/chat/completions",
"wire_model":"deepseek/deepseek-v4.1-flash",
"reasoning_effort":"low",
"translation_completion_tokens":24000,
"auxiliary_completion_tokens":8000,
"accounting":"command_code_credit_equivalent",
"pricing_unit":"usd_credit_equivalent_per_1m_tokens",
"revision":"production-provider-runtime-v1",
"logical_model":"deepseek-flash",
"model_version":"DeepSeek-V4.1-Flash",
"reasoning_by_operation":{"translation":"low","state_extraction":"low",
"glossary_review":"low","periodic_audit":"low","state_json_repair":"low","terminology_repair":"low"},
"translation_budget_policy_sha256":"13d8c63f629f26f6e4bc098deba80a865075276859ab858a3e2c8ddc03b0539a",
"auxiliary_budget_policy_sha256":"1246c5a6ec736e5e3246c02536eb72cb6a088a99f08f0bf5bcb2795d91f1a4e9",
"accounting_policy_sha256":"1115201387123ed34d499c8bb1a6ee9b7401f49b05857496b337773b490f9ac0",
"pricing_version":"command-code-provider-public-ratecard-2026-09-19-v1",
"provider_invoice_claimed":false,
"scheduler_mode":"provider_unrestricted"
}'''
HISTORICAL_GOAT_PROVIDER_IDENTITY_SHA256 = "f0706bfd4d7fd953ad64abdefcc438a37632225afa87564a6f58537b17ee9cd8"


def historical_goat_provider_identity(selection="command_code_provider", model="v41-flash"):
    """Frozen v1 validation snapshot; never a source for active request controls."""
    if selection != "command_code_provider" or model not in ("v41-flash", "deepseek-flash"):
        raise ProductionProviderError("historical GOAT identity requires v41-flash GOAT")
    return json.loads(_HISTORICAL_GOAT_V1_JSON)


def historical_capped_goat_provider_identity():
    """Exact main 65f6ae5 50K/8K identity; explicitly distinct from v1 and v3."""
    from production_goat_policy import historical_policy_identity_v1
    result = historical_goat_provider_identity()
    result.update(revision="production-provider-runtime-v2", translation_completion_tokens=50000,
        translation_budget_policy_revision="goat-production-low-50k-v1",
        translation_budget_policy_sha256=hashlib.sha256(json.dumps(historical_policy_identity_v1(),
            ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest())
    return result


class HistoricalGoatManualReviewRequired(ProductionProviderError):
    """Recognized historical evidence requires explicit migration review."""

    code = "GOAT_HISTORICAL_MANUAL_REVIEW"

    def __init__(self):
        super().__init__(self.code + ": pre-50K GOAT evidence preserved; automatic migration/replay "
                         "under v2 is disabled; manual review required before any send")


def recovery_module(meta):
    revision = meta.get("translation_recovery_policy_revision")
    if revision == "goat-reasoning-saturation-recovery-v1":
        import production_goat_saturation_recovery as recovery
    elif revision == "goat-cap-failure-recovery-v2":
        import production_goat_cap_recovery as recovery
    else:
        raise ProductionProviderError("unknown GOAT recovery revision")
    return recovery


def pending_provider_compatibility(meta, identity):
    if (identity.get("revision") == "production-provider-runtime-v3"
        and meta.get("translation_provider_identity") == historical_capped_goat_provider_identity()):
        raise ProductionProviderError("GOAT capped pending requires dedicated auxiliary policy migration planning")
    if (identity.get("provider") == "command_code_provider"
        and identity != historical_goat_provider_identity()
        and meta.get("translation_provider_identity") == historical_goat_provider_identity()
        and meta.get("translation_recovery_policy_revision") != "goat-reasoning-saturation-recovery-v1"):
        raise HistoricalGoatManualReviewRequired()
    if (identity.get("provider") == PROVIDER_COMMAND_CODE
        and meta.get("translation_recovery_policy_revision") == "goat-reasoning-saturation-recovery-v1"
        and meta.get("translation_provider_identity") == historical_goat_provider_identity()):
        recovery_module(meta).validate_provenance(meta)
        identity = historical_goat_provider_identity()
    declared = meta.get("translation_provider")
    provider = identity["provider"]
    if declared is None:
        # Historical missing provider never confers GOAT authorization.
        declared = PROVIDER_DIRECT_DEEPSEEK
    if declared != provider:
        raise ProductionProviderError("pending provider disagrees with requested provider; migration is forbidden")
    if provider == PROVIDER_COMMAND_CODE:
        if meta.get("translation_provider_identity") != identity:
            raise ProductionProviderError("GOAT pending provider policy identity drift")
        if meta.get("translation_wire_model") != identity["wire_model"]:
            raise ProductionProviderError("GOAT pending wire model drift")
        if meta.get("translation_model") != identity["logical_model"]:
            raise ProductionProviderError("GOAT pending logical model drift")
        digest = meta.get("translation_request_sha256")
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ProductionProviderError("GOAT pending lacks immutable translation request SHA")
    return {"pending_provider": declared, "effective_provider": provider,
            "provider_compatible": True}


def inspect_pending_provider(pending_dir, identity, *, ignored_pending_chapters=()):
    records = []
    for path in sorted(Path(pending_dir).glob("*.json")):
        if path.stem.isdigit() and int(path.stem) not in ignored_pending_chapters:
            meta = json.loads(path.read_text(encoding="utf-8"))
            compatibility = pending_provider_compatibility(meta, identity)
            if identity["provider"] == PROVIDER_COMMAND_CODE:
                pending_identity = meta.get("translation_provider_identity")
                from production_paid_request_journal import _validate_common_record
                project_root = Path(pending_dir).resolve().parent.parent
                journal = project_root / "data/provider_request_journal" / (path.stem + ".chapter_translation.json")
                if not journal.is_file():
                    raise ProductionProviderError("GOAT pending lacks original paid translation journal")
                record = _validate_common_record(json.loads(journal.read_text(encoding="utf-8")), path=journal)
                from novel_project import load_novel_config, NovelProject
                from chapter_library import index_chapter_library
                config = load_novel_config(project_root / "novel.json")
                project = NovelProject(project_root.parent.parent, project_root, config)
                sources = index_chapter_library(json.loads(project.chapters_file.read_text(encoding="utf-8")),
                    config.final_chapter, chapter_numbering=config.chapter_numbering)
                source = sources.get(int(path.stem), {}).get("text")
                if (record.get("novel_id") != config.id or record.get("profile_id") != config.profile
                    or not isinstance(source, str)
                    or record.get("source_sha256") != hashlib.sha256(source.encode("utf-8")).hexdigest()):
                    raise ProductionProviderError("GOAT pending journal novel/profile/source provenance mismatch")
                if (record.get("status") != "response_recorded"
                    or record.get("provider") != PROVIDER_COMMAND_CODE
                    or record.get("model") != identity["wire_model"]
                    or record.get("provider_policy") != pending_identity
                    or record.get("chapter") != int(path.stem)
                    or record.get("operation_kind") != "chapter_translation"
                    or record.get("wire_request_sha256") != meta["translation_request_sha256"]):
                    raise ProductionProviderError("GOAT pending translation journal provenance mismatch")
            if meta.get("goat_reasoning_saturation_recovery_used") is True:
                from production_translation_recovery_prefix import load_recorded_recovery_prefix
                prefix_meta = dict(meta, stage="translation_recovery_recorded")
                # QA repairs may alter pending text; the original paid chain is still proven.
                load_recorded_recovery_prefix(path, prefix_meta, verify_text=False)
            records.append(compatibility)
    return records


def goat_provenance(identity, request_sha256):
    return {"translation_provider": PROVIDER_COMMAND_CODE,
            "translation_provider_identity": copy.deepcopy(identity),
            "translation_provider_base_url": identity["base_url"],
            "translation_wire_model": identity["wire_model"],
            "translation_request_sha256": request_sha256}


def authorization_provider(selection, model, max_cost_usd, max_credit_equivalent_usd):
    identity = provider_identity(selection, model)
    if identity["provider"] == PROVIDER_COMMAND_CODE:
        if max_credit_equivalent_usd is None:
            raise ProductionProviderError("GOAT authorization requires explicit max_credit_equivalent_usd")
        if max_cost_usd is not None:
            raise ProductionProviderError("GOAT authorization forbids direct max_cost_usd units")
        if (isinstance(max_credit_equivalent_usd, bool)
            or not isinstance(max_credit_equivalent_usd, (int, float))
            or not math.isfinite(max_credit_equivalent_usd) or max_credit_equivalent_usd <= 0):
            raise ProductionProviderError("GOAT credit-equivalent ceiling must be finite and positive")
        ceiling = max_credit_equivalent_usd
    else:
        if max_credit_equivalent_usd is not None:
            raise ProductionProviderError("direct authorization forbids credit-equivalent units")
        ceiling = max_cost_usd
    return identity, ceiling


def canonical_pending_provenance(meta, *, approved_identity=None):
    """Canonical model/provider record for partial commit and cleanup proofs."""
    from translation_model_config import pending_translation_model_record
    record = pending_translation_model_record(meta["translation_model"])
    if meta.get("translation_provider") == PROVIDER_COMMAND_CODE:
        recorded = meta.get("translation_provider_identity")
        # Canonical provenance is historical description, not send permission.
        # The production launcher still refuses this identity outside migration.
        identity = approved_identity or (recorded if recorded == historical_capped_goat_provider_identity()
            else provider_identity(PROVIDER_COMMAND_CODE, meta["translation_model"]))
        pending_provider_compatibility(meta, identity)
        identity = meta["translation_provider_identity"]
        record.update(goat_provenance(identity, meta["translation_request_sha256"]))
        record.update(translation_max_tokens=identity["translation_completion_tokens"],
                      translation_thinking=identity["reasoning_effort"],
                      translation_pricing_version=identity["pricing_version"])
        from production_goat_state_policy_migration import MARKER
        if MARKER in meta:
            record[MARKER] = copy.deepcopy(meta[MARKER])
        if meta.get("goat_reasoning_saturation_recovery_used") is True:
            recovery_module(meta).validate_provenance(meta)
            keys = ("goat_reasoning_saturation_recovery_used", "translation_recovery_used",
                "initial_translation_operation_id", "recovery_translation_operation_id",
                "translation_recovery_policy_revision", "translation_recovery_policy_sha256",
                "translation_recovery_strategy", "translation_recovery_max_tokens")
            record.update({key: copy.deepcopy(value) for key, value in meta.items()
                           if key in keys or key in ("production_policy_revision", "production_policy_sha256", "cap_failure_shape", "reasoning_effort") or key.startswith(("initial_", "recovery_"))})
    elif meta.get("translation_provider") not in (None, PROVIDER_DIRECT_DEEPSEEK):
        raise ProductionProviderError("unknown pending provider provenance")
    return record


def install_goat_provenance(core_module, config, pending_dir, identity):
    from translation_model_config import pending_translation_model_record
    root = Path(pending_dir).resolve()
    qa_root = root.parent / "qa"
    original_text = core_module.atomic_write_text
    original_json = core_module.atomic_write_json
    generated = set()

    def write_text(path, text, encoding="utf-8"):
        path = Path(path)
        if path.resolve().parent == root and path.suffix == ".md" and path.stem.isdigit() and not path.exists():
            generated.add(int(path.stem))
        return original_text(path, text, encoding)

    def write_json(path, data):
        candidate = Path(path)
        if candidate.stem.isdigit() and candidate.suffix == ".json" and isinstance(data, dict):
            parent = candidate.resolve().parent
            if parent == root:
                if int(candidate.stem) in generated and "translation_provider" not in data:
                    digest = data.get("translation_stats", {}).get("request_sha256")
                    if not digest:
                        raise ProductionProviderError("new GOAT pending lacks translation transport proof")
                    data = {**data, **pending_translation_model_record(config.model), **goat_provenance(identity, digest)}
                    data.update(translation_max_tokens=identity["translation_completion_tokens"],
                                translation_thinking=identity["reasoning_effort"],
                                translation_pricing_version=identity["pricing_version"])
                pending_provider_compatibility(data, identity)
            elif parent == qa_root:
                pending = root / candidate.name
                if not pending.is_file():
                    raise ProductionProviderError("new GOAT committed QA requires pending provenance")
                meta = json.loads(pending.read_text(encoding="utf-8"))
                pending_provider_compatibility(meta, identity)
                data = {**data, **canonical_pending_provenance(meta, approved_identity=identity)}
        return original_json(path, data)

    core_module.atomic_write_text = write_text
    core_module.atomic_write_json = write_json
    return {"mode": "goat_provider_provenance", "model": config.model,
            "qa_root": qa_root, "excluded_chapters": set(),
            "generated_chapters": generated, "original_atomic_write_json": original_json}
