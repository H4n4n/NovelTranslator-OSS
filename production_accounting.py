"""Provider-aware cumulative accounting; legacy unbound state is direct history."""
import math
import hashlib
import json
import re
from copy import deepcopy
from production_provider_contract import (
    PROVIDER_COMMAND_CODE, PROVIDER_DIRECT_DEEPSEEK,
    ACCOUNTING_COMMAND_CODE_CREDIT, ACCOUNTING_DEEPSEEK_USD,
    PRICING_UNIT_COMMAND_CODE, PRICING_UNIT_DEEPSEEK,
)
from production_provider_errors import ProductionProviderError
from benchmark_longitudinal_g7d_accounting_policy import PRICING_VERSION as GOAT_VERSION

TOTAL_FIELDS = ("total_prompt_tokens", "total_completion_tokens", "total_cache_hit_tokens",
                "total_cache_miss_tokens", "estimated_cost_usd", "rate_correct_cost_usd")
GOAT_METADATA = dict(provider=PROVIDER_COMMAND_CODE, accounting=ACCOUNTING_COMMAND_CODE_CREDIT,
    pricing_unit=PRICING_UNIT_COMMAND_CODE, pricing_version=GOAT_VERSION, provider_invoice_claimed=False)

ACCOUNTING_SCHEMA_VERSION = 2
MIGRATION_REVISION = "direct-to-goat-accounting-epoch-v1"
HISTORICAL_EPOCH_REVISION = "sealed-direct-accounting-epoch-v1"
ACTIVE_EPOCH_REVISION = "active-goat-accounting-epoch-v1"
EPOCH_FIELDS = ("accounting_schema_version", "accounting_history", "active_accounting_epoch")
LEGACY_FIELDS = TOTAL_FIELDS + ("provider", "accounting", "pricing_unit", "pricing_version", "provider_invoice_claimed") + (
    "rate_correct_cost_start_chapter", "accounted_api_operation_ids")
# Durable migration-v1 identity: never derive this snapshot from current policy.
# Future migration semantics require a new revision and independently pinned SHA.
_MIGRATION_V1_POLICY_JSON = '''{
  "revision": "direct-to-goat-accounting-epoch-v1",
  "accounting_schema_version": 2,
  "historical_revision": "sealed-direct-accounting-epoch-v1",
  "active_revision": "active-goat-accounting-epoch-v1",
  "direction": "direct_deepseek->command_code_provider",
  "total_fields": ["total_prompt_tokens", "total_completion_tokens", "total_cache_hit_tokens",
    "total_cache_miss_tokens", "estimated_cost_usd", "rate_correct_cost_usd"],
  "active_metadata": {
    "provider": "command_code_provider",
    "accounting": "command_code_credit_equivalent",
    "pricing_unit": "usd_credit_equivalent_per_1m_tokens",
    "pricing_version": "command-code-provider-public-ratecard-2026-09-19-v1",
    "provider_invoice_claimed": false
  },
  "preserve_global_operation_ids": true,
  "any_pending_blocks": true,
  "ambiguous_journal_blocks": true,
  "history_count": 1,
  "zero_direct_adoption": "no-history",
  "currency_conversion": false,
  "boundary": "fixed-at-migration-monotonic-progress",
  "preserve_fields": ["total_prompt_tokens", "total_completion_tokens", "total_cache_hit_tokens",
    "total_cache_miss_tokens", "estimated_cost_usd", "rate_correct_cost_usd", "provider",
    "accounting", "pricing_unit", "pricing_version", "provider_invoice_claimed",
    "rate_correct_cost_start_chapter", "accounted_api_operation_ids"]
}'''
MIGRATION_POLICY_SHA256 = "9c2a545f43369a3371e65adab4525e776e8485a485cb99a3e14e53b8e104e1f5"


def canonical_sha256(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def migration_v1_policy_identity():
    """Return a fresh frozen-v1 identity; edits cannot silently redefine v1."""
    policy = json.loads(_MIGRATION_V1_POLICY_JSON)
    if canonical_sha256(policy) != MIGRATION_POLICY_SHA256:
        raise ProductionProviderError("frozen migration-v1 policy SHA mismatch; new semantics require a new revision")
    return policy


# Compatibility inspection surface; historical validation uses the fresh snapshot.
MIGRATION_POLICY = migration_v1_policy_identity()


def _require(condition, message):
    if not condition:
        raise ProductionProviderError("accounting epoch: " + message)


def _boundary(progress):
    last, start = progress.get("last_completed"), progress.get("next_chapter")
    _require(type(last) is int and last >= 0 and type(start) is int and start == last + 1,
             "invalid chapter boundary")
    completed = progress.get("completed")
    _require(isinstance(completed, list) and all(type(n) is int for n in completed)
             and completed == list(range(1, last + 1)), "invalid completed boundary")
    return last, start


def migrate_direct_progress(progress, source_progress_sha256):
    """Pure, one-way construction. Never called implicitly by bind/preflight."""
    migration_metadata = migration_v1_policy_identity()["active_metadata"]
    _require(not any(key in progress for key in EPOCH_FIELDS), "already versioned")
    _require(validate_progress_accounting(progress) == PROVIDER_DIRECT_DEEPSEEK, "not direct")
    _require(any(progress.get(key, 0) != 0 for key in TOTAL_FIELDS), "zero direct needs no migration")
    last, start = _boundary(progress)
    presence = {key: key in progress for key in LEGACY_FIELDS}
    epoch = dict(schema_version=1, revision=HISTORICAL_EPOCH_REVISION, epoch_index=0,
        normalized_provider=PROVIDER_DIRECT_DEEPSEEK, normalized_accounting=ACCOUNTING_DEEPSEEK_USD,
        normalized_pricing_unit=PRICING_UNIT_DEEPSEEK,
        totals={key: deepcopy(progress[key]) for key in TOTAL_FIELDS if key in progress},
        legacy_field_presence=presence,
        legacy_metadata={key: deepcopy(progress[key]) for key in migration_metadata if key in progress},
        closed_after_completed_chapter=last, next_chapter_at_close=start,
        source_progress_sha256=source_progress_sha256)
    for key in ("pricing_version", "rate_correct_cost_start_chapter", "accounted_api_operation_ids"):
        if key in progress:
            epoch[key if key != "accounted_api_operation_ids" else "accounted_api_operation_ids_at_close"] = deepcopy(progress[key])
    epoch["epoch_payload_sha256"] = canonical_sha256(epoch)
    active = dict(schema_version=1, revision=ACTIVE_EPOCH_REVISION, epoch_index=1,
        **migration_metadata, start_after_completed_chapter=last, start_chapter=start,
        migration_revision=MIGRATION_REVISION, migration_policy_sha256=MIGRATION_POLICY_SHA256,
        historical_epoch_payload_sha256=epoch["epoch_payload_sha256"])
    result = deepcopy(progress)
    result.update(migration_metadata)
    result.update({key: 0.0 if key.endswith("usd") else 0 for key in TOTAL_FIELDS})
    result.update(accounting_schema_version=2, accounting_history=[epoch],
        active_accounting_epoch=active, rate_correct_cost_start_chapter=start)
    validate_progress_accounting(result)
    return result


def _validate_epochs(progress):
    if not any(key in progress for key in EPOCH_FIELDS):
        return
    migration_metadata = migration_v1_policy_identity()["active_metadata"]
    _require(type(progress.get("accounting_schema_version")) is int
             and progress["accounting_schema_version"] == 2, "unknown schema")
    history, active = progress.get("accounting_history"), progress.get("active_accounting_epoch")
    _require(isinstance(history, list) and len(history) == 1 and isinstance(history[0], dict), "requires one sealed epoch")
    epoch = history[0]
    payload = {key: value for key, value in epoch.items() if key != "epoch_payload_sha256"}
    _require(epoch.get("epoch_payload_sha256") == canonical_sha256(payload), "invalid historical payload SHA")
    _require(isinstance(epoch.get("source_progress_sha256"), str)
             and re.fullmatch(r"[0-9a-f]{64}", epoch["source_progress_sha256"]), "invalid source progress SHA")
    _require(type(epoch.get("schema_version")) is int and epoch["schema_version"] == 1
             and type(epoch.get("epoch_index")) is int and epoch["epoch_index"] == 0
             and epoch.get("revision") == HISTORICAL_EPOCH_REVISION, "unknown historical identity")
    _require(epoch.get("normalized_provider") == PROVIDER_DIRECT_DEEPSEEK
             and epoch.get("normalized_accounting") == ACCOUNTING_DEEPSEEK_USD
             and epoch.get("normalized_pricing_unit") == PRICING_UNIT_DEEPSEEK, "historical provider mismatch")
    presence, metadata, totals = epoch.get("legacy_field_presence"), epoch.get("legacy_metadata"), epoch.get("totals")
    _require(isinstance(presence, dict) and set(presence) == set(LEGACY_FIELDS)
             and all(type(v) is bool for v in presence.values()), "invalid field presence")
    _require(isinstance(metadata, dict) and set(metadata) == {k for k in migration_metadata if presence[k]}, "invalid legacy metadata")
    _require(isinstance(totals, dict) and set(totals) == {k for k in TOTAL_FIELDS if presence[k]}, "invalid historical totals")
    legacy = {**metadata, **totals}
    expected_keys = set(payload) - {"pricing_version", "rate_correct_cost_start_chapter", "accounted_api_operation_ids_at_close"}
    _require(expected_keys == {"schema_version", "revision", "epoch_index", "normalized_provider",
        "normalized_accounting", "normalized_pricing_unit", "totals", "legacy_field_presence",
        "legacy_metadata", "closed_after_completed_chapter", "next_chapter_at_close", "source_progress_sha256"}, "unknown historical fields")
    for key in ("pricing_version", "rate_correct_cost_start_chapter", "accounted_api_operation_ids"):
        saved_key = key if key != "accounted_api_operation_ids" else "accounted_api_operation_ids_at_close"
        _require((saved_key in epoch) == presence[key], "historical presence mismatch")
        if presence[key]:
            legacy[key] = epoch[saved_key]
    _require(not presence["pricing_version"] or metadata["pricing_version"] == epoch["pricing_version"], "legacy pricing mismatch")
    _require(validate_progress_accounting(legacy) == PROVIDER_DIRECT_DEEPSEEK, "history is not direct")
    _require(any(legacy.get(k, 0) != 0 for k in TOTAL_FIELDS), "fake zero historical epoch")
    last, next_chapter = _boundary(progress)
    closed, start = epoch.get("closed_after_completed_chapter"), epoch.get("next_chapter_at_close")
    _require(type(closed) is int and closed >= 0 and type(start) is int and start == closed + 1
             and last >= closed and next_chapter >= start, "altered migration boundary")
    if presence["rate_correct_cost_start_chapter"]:
        old_start = epoch["rate_correct_cost_start_chapter"]
        _require(type(old_start) is int and 1 <= old_start <= start, "invalid historical cost boundary")
    if presence["accounted_api_operation_ids"]:
        ids = epoch["accounted_api_operation_ids_at_close"]
        current = progress.get("accounted_api_operation_ids")
        _require(isinstance(ids, list) and all(isinstance(n, str) and n for n in ids)
                 and isinstance(current, list) and current[:len(ids)] == ids, "global operation registry lost")
    expected = dict(schema_version=1, revision=ACTIVE_EPOCH_REVISION, epoch_index=1,
        **migration_metadata, start_after_completed_chapter=closed, start_chapter=start,
        migration_revision=MIGRATION_REVISION, migration_policy_sha256=MIGRATION_POLICY_SHA256,
        historical_epoch_payload_sha256=epoch["epoch_payload_sha256"])
    _require(isinstance(active, dict) and canonical_sha256(active) == canonical_sha256(expected), "active identity/boundary mismatch")
    _require(all(k in progress and progress[k] == v for k, v in migration_metadata.items())
             and progress.get("provider_invoice_claimed") is False, "active/top-level mismatch")
    _require(all(k in progress for k in TOTAL_FIELDS)
             and type(progress.get("rate_correct_cost_start_chapter")) is int
             and progress["rate_correct_cost_start_chapter"] == start, "active counters/boundary missing")


def accounting_report_lines(progress):
    """Explicit units; never compute a cross-provider lifetime sum."""
    validate_progress_accounting(progress)
    lines = []
    if "accounting_history" in progress:
        totals = progress["accounting_history"][0]["totals"]
        lines.append("Sealed historical direct DeepSeek accounting (USD): "
            f"estimated ${totals.get('estimated_cost_usd', 0):.6f}; "
            f"rate-correct ${totals.get('rate_correct_cost_usd', 0):.6f}")
    if progress.get("provider") == PROVIDER_COMMAND_CODE:
        lines.append("Active GOAT accounting (USD credit-equivalent; provider_invoice_claimed=false): "
            f"estimated ${progress.get('estimated_cost_usd', 0):.6f}; "
            f"rate-correct ${progress.get('rate_correct_cost_usd', 0):.6f}")
    return lines


def validate_progress_accounting(progress, *, expected_provider=None, direct_pricing_version=None):
    if not isinstance(progress, dict):
        raise ProductionProviderError("progress accounting must be an object")
    _validate_epochs(progress)
    provider = progress.get("provider", PROVIDER_DIRECT_DEEPSEEK)
    if provider == PROVIDER_COMMAND_CODE:
        if any(progress.get(key) != value for key, value in GOAT_METADATA.items()) or progress.get("provider_invoice_claimed") is not False:
            raise ProductionProviderError("GOAT progress accounting metadata mismatch")
    elif provider == PROVIDER_DIRECT_DEEPSEEK:
        if progress.get("pricing_version") == GOAT_VERSION:
            raise ProductionProviderError("GOAT metadata cannot be accounted as direct DeepSeek")
        for key, value in (("accounting", ACCOUNTING_DEEPSEEK_USD), ("pricing_unit", PRICING_UNIT_DEEPSEEK)):
            if key in progress and progress[key] != value:
                raise ProductionProviderError("direct progress accounting metadata mismatch")
        if direct_pricing_version is not None and progress.get("pricing_version") != direct_pricing_version:
            raise ProductionProviderError("progress.pricing_version does not match the production pricing version")
    else:
        raise ProductionProviderError("unknown progress accounting provider")
    nonzero = False
    for key in TOTAL_FIELDS:
        value = progress.get(key, 0)
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ProductionProviderError("invalid cumulative accounting total: " + key)
        nonzero |= value != 0
    if expected_provider is not None and provider != expected_provider:
        # Only legacy/direct zero state may adopt GOAT. Explicit GOAT stays bound.
        if nonzero or provider == PROVIDER_COMMAND_CODE:
            raise ProductionProviderError("cross-provider cumulative accounting mixing forbidden")
    return provider


def bind_progress_accounting(progress, stats):
    provider = stats.get("provider", PROVIDER_COMMAND_CODE if
        stats.get("accounting") == ACCOUNTING_COMMAND_CODE_CREDIT else PROVIDER_DIRECT_DEEPSEEK)
    if provider not in (PROVIDER_DIRECT_DEEPSEEK, PROVIDER_COMMAND_CODE):
        raise ProductionProviderError("unknown operation accounting provider")
    if provider == PROVIDER_DIRECT_DEEPSEEK:
        for key, value in (("accounting", ACCOUNTING_DEEPSEEK_USD), ("pricing_unit", PRICING_UNIT_DEEPSEEK)):
            if key in stats and stats[key] != value:
                raise ProductionProviderError("direct operation accounting metadata mismatch")
        if stats.get("pricing_version") == GOAT_VERSION:
            raise ProductionProviderError("GOAT rate card cannot be accounted as direct DeepSeek")
    validate_progress_accounting(progress, expected_provider=provider)
    if provider == PROVIDER_COMMAND_CODE:
        if any(stats.get(key) != value for key, value in GOAT_METADATA.items()):
            raise ProductionProviderError("GOAT response accounting metadata mismatch")
        progress.update(GOAT_METADATA)


def combine_goat_stats(combine_numeric, *items):
    from benchmark_longitudinal_g7j_reasoning_budget import reasoning_usage_diagnostics
    out = combine_numeric(*items)
    paid = [item for item in items if item and (item.get("provider") == PROVIDER_COMMAND_CODE
            or any(item.get(key, 0) for key in ("prompt_tokens", "completion_tokens", "cost_usd", "rate_correct_cost_usd")))]
    for item in paid:
        if any(item.get(key) != value for key, value in GOAT_METADATA.items()):
            raise ProductionProviderError("mixed GOAT operation accounting forbidden")
        reasoning_usage_diagnostics(completion_tokens=item["completion_tokens"],
            reasoning_tokens=item.get("reasoning_tokens"),
            reasoning_tokens_exposed=item.get("reasoning_tokens_exposed", False))
    if paid:
        # Keep individual rate snapshots when repairs span multiple requests.
        # Flatten prior aggregates so validation never counts a subtotal twice.
        out["accounting_components"] = [deepcopy(component) for item in paid
            for component in item.get("accounting_components", [item])]
        out.update(GOAT_METADATA)
        exposed = all(item.get("reasoning_tokens_exposed") is True for item in paid)
        out.update(reasoning_usage_diagnostics(completion_tokens=out["completion_tokens"],
            reasoning_tokens=sum(item["reasoning_tokens"] for item in paid) if exposed else None,
            reasoning_tokens_exposed=exposed))
    return out


def validate_goat_stats(stats):
    """Validate a saved single-call GOAT envelope against the frozen rate card."""
    from decimal import Decimal
    from benchmark_longitudinal_g7d_accounting_policy import pricing_snapshot
    from production_provider_contract import COMMAND_CODE_DEEPSEEK_V41_FLASH
    from benchmark_longitudinal_g7j_reasoning_budget import reasoning_usage_diagnostics
    if any(stats.get(key) != value for key, value in GOAT_METADATA.items() if key != "provider"):
        raise ProductionProviderError("GOAT stats accounting metadata mismatch")
    if stats.get("provider", PROVIDER_COMMAND_CODE) != PROVIDER_COMMAND_CODE or stats.get("provider_invoice_claimed") is not False:
        raise ProductionProviderError("GOAT stats provider mismatch")
    if "accounting_components" in stats:
        components = stats["accounting_components"]
        if not isinstance(components, list) or not components or any(
            not isinstance(item, dict) or "accounting_components" in item for item in components):
            raise ProductionProviderError("GOAT invalid accounting components")
        for item in components:
            validate_goat_stats(item)
        for key in ("prompt_tokens", "completion_tokens", "cache_hit_tokens", "cache_miss_tokens", "cost_usd", "rate_correct_cost_usd"):
            value = stats.get(key)
            if type(value) not in (int, float) or not math.isfinite(value) or abs(value - sum(item[key] for item in components)) > 1e-12:
                raise ProductionProviderError("GOAT accounting component sum mismatch")
        exposed = all(item.get("reasoning_tokens_exposed") is True for item in components)
        expected = reasoning_usage_diagnostics(completion_tokens=stats["completion_tokens"],
            reasoning_tokens=sum(item["reasoning_tokens"] for item in components) if exposed else None,
            reasoning_tokens_exposed=exposed)
        if any(stats.get(key) != value for key, value in expected.items()) or (not exposed and "visible_completion_tokens" in stats):
            raise ProductionProviderError("GOAT aggregate reasoning diagnostic mismatch")
        return
    for key in ("prompt_tokens", "completion_tokens", "cache_hit_tokens", "cache_miss_tokens"):
        if type(stats.get(key)) is not int or stats[key] < 0:
            raise ProductionProviderError("GOAT stats invalid token count")
    rates = pricing_snapshot(COMMAND_CODE_DEEPSEEK_V41_FLASH, stats["request_started_utc"])
    for key in ("pricing_period", "cache_hit_rate_per_million", "cache_miss_rate_per_million", "output_rate_per_million"):
        if stats.get(key) != rates[key]:
            raise ProductionProviderError("GOAT stats frozen rate mismatch")
    cost = float((Decimal(stats["cache_hit_tokens"]) * Decimal(str(rates["cache_hit_rate_per_million"]))
        + Decimal(stats["cache_miss_tokens"]) * Decimal(str(rates["cache_miss_rate_per_million"]))
        + Decimal(stats["completion_tokens"]) * Decimal(str(rates["output_rate_per_million"]))) / Decimal(1_000_000))
    for key in ("cost_usd", "rate_correct_cost_usd"):
        if type(stats.get(key)) not in (int, float) or not math.isfinite(stats[key]) or abs(stats[key] - cost) > 1e-12:
            raise ProductionProviderError("GOAT stats cost mismatch")
    diagnostic = reasoning_usage_diagnostics(completion_tokens=stats["completion_tokens"],
        reasoning_tokens=stats.get("reasoning_tokens"), reasoning_tokens_exposed=stats.get("reasoning_tokens_exposed", False))
    if "visible_completion_tokens" in stats and stats["visible_completion_tokens"] != diagnostic.get("visible_completion_tokens"):
        raise ProductionProviderError("GOAT visible completion diagnostic mismatch")
