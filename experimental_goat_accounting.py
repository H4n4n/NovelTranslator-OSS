"""Experiment-only compatibility counters; durable budget.json is CE authority."""
import copy
from decimal import Decimal
from types import FunctionType
from experimental_goat_policy import ExperimentError, PROVIDER, decimal

FIELDS = {"prompt_tokens":"total_prompt_tokens", "completion_tokens":"total_completion_tokens",
    "cache_hit_tokens":"total_cache_hit_tokens", "cache_miss_tokens":"total_cache_miss_tokens",
    "cost_usd":"estimated_cost_usd", "rate_correct_cost_usd":"rate_correct_cost_usd"}


def accounting_identity(model, plan_sha256):
    return {"provider":PROVIDER, "accounting":"experimental_command_code_credit_equivalent",
        "pricing_unit":"usd_credit_equivalent_per_1m_tokens", "pricing_version":model.sha256,
        "provider_invoice_claimed":False, "experimental_plan_sha256":plan_sha256}


def install_accounting(module, model, plan_sha256):
    """Bind only this engine instance; production accounting module stays untouched."""
    identity = accounting_identity(model,plan_sha256)
    if any(module.progress.get(k) != v for k,v in identity.items()):
        raise ExperimentError("isolated progress accounting identity mismatch")

    def valid_stats(stats):
        if not any(stats.get(k,0) for k in FIELDS):
            return decimal("0")
        if any(stats.get(k) != v for k,v in identity.items() if k != "experimental_plan_sha256"):
            raise ExperimentError("experimental stats accounting identity mismatch")
        for key in ("prompt_tokens","completion_tokens","cache_hit_tokens","cache_miss_tokens"):
            if type(stats.get(key)) is not int or stats[key] < 0:
                raise ExperimentError("invalid saved experimental tokens")
        if stats["cache_hit_tokens"] + stats["cache_miss_tokens"] != stats["prompt_tokens"]:
            raise ExperimentError("saved cache accounting mismatch")
        ce = model.charge(stats["prompt_tokens"],stats["completion_tokens"],stats["cache_hit_tokens"])
        if decimal(stats.get("cost_credit_equivalent")) != ce:
            raise ExperimentError("saved CE accounting mismatch")
        return ce

    def combine(*items):
        out = module.zero_stats()
        ce = decimal("0")
        for item in items:
            if item:
                ce += valid_stats(item)
                for key in FIELDS:
                    out[key] += item.get(key,0)
        out.update({k:v for k,v in identity.items() if k != "experimental_plan_sha256"})
        out.update(cost_credit_equivalent=str(ce),cost_usd=float(ce),rate_correct_cost_usd=float(ce))
        return out

    def totals(progress,stats):
        if any(progress.get(k) != v for k,v in identity.items()):
            raise ExperimentError("experimental progress identity drift")
        ce = valid_stats(stats)
        for key,total in FIELDS.items():
            progress[total] = progress.get(total,0) + stats.get(key,0)
        progress["experiment_accepted_ce"] = str(decimal(progress.get("experiment_accepted_ce","0")) + ce)
        return progress

    def once(progress,operation_id,stats):
        if not isinstance(operation_id,str) or not operation_id:
            raise ExperimentError("auxiliary operation ID required")
        recorded = progress.setdefault("accounted_api_operation_ids",[])
        if operation_id in recorded:
            return False
        totals(progress,stats)
        recorded.append(operation_id)
        return True

    # Reuse the exact shared chapter-progress algorithm with a private globals map.
    original = module.commit_chapter_accounting
    globals_copy = dict(original.__globals__, add_accounting_totals=totals)
    module.commit_chapter_accounting = FunctionType(original.__code__,globals_copy,
                                                    original.__name__,original.__defaults__,original.__closure__)
    module.accounting_report_lines = lambda progress:[f"Experiment accepted CE (compatibility counter): {progress.get('experiment_accepted_ce','0')}; budget.json remains authoritative; provider_invoice_claimed=false"]
    from functools import partial
    module.run_chapter_glossary_review = partial(module.run_chapter_glossary_review, combine_stats=combine)
    original_audit = module.call_periodic_audit
    def audit_accounting(*args, **kwargs):
        result, numeric, chunking = original_audit(*args, **kwargs)
        checkpoint_path = chunking.get("checkpoint_file")
        if checkpoint_path:
            import json
            from experimental_goat_policy import local_path
            checkpoint = json.loads(local_path(checkpoint_path).read_text(encoding="utf-8"))
            records = checkpoint["attempt_records"]
            aggregate = combine(*(r.get("stats",{}) for r in records))
            aggregate["api_request_count"] = len(records)
        else:
            aggregate = combine(numeric)
        return result, aggregate, chunking
    module.call_periodic_audit = audit_accounting
    module.combine_stats = combine
    module.add_accounting_totals_once = once
    module.reprice_saved_stats_if_needed = lambda stats:(valid_stats(stats),copy.deepcopy(stats))[1]
