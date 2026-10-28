"""Read-only experimental paid-audit proof; no transport or ledger settlement.

The immutable experiment plan records the full provider/model policy. Journals
bind that plan and model policy by hash; budget receipts bind the raw response.
The shared audit proof owns source candidates, topology, parsing and corrections.
"""
import json
from pathlib import Path

import glossary_audit as audit
from experimental_goat_policy import ModelPolicy, canonical, decimal, digest, local_path, text_sha
from experimental_goat_runtime import ExperimentalRuntime, normalize_request, accept_response
from experimental_goat_budget import ExperimentBudget
from experimental_goat_accounting import accounting_identity
from production_paid_request_journal import request_identity, load_matching_journal
from production_periodic_audit_finalization import require, file_sha


KINDS = {
    "audit": ("periodic_audit", "request_periodic_audit_chunk"),
    "json_repair": ("periodic_audit_json_repair", "repair_periodic_audit_chunk"),
    "semantic_correction": ("periodic_audit_semantic_correction", "correct_periodic_audit_chunk_semantics"),
}


def owner_for(module):
    runtime = getattr(module, "_experimental_runtime", None)
    experimental = module.progress.get("accounting") == "experimental_command_code_credit_equivalent"
    if runtime is None and not experimental:
        return None
    return ExperimentalAuditProof(module, runtime)


class ExperimentalAuditProof:
    def __init__(self, module, runtime):
        from experimental_goat_experiment import validate_root
        require(type(runtime) is ExperimentalRuntime, "experimental audit requires installed runtime")
        self.module = module
        self.track = module.ACTIVE_NOVEL_CONFIG.id
        self.root = local_path(Path(module.PROGRESS_FILE).absolute()).parent.parent
        require(self.root == runtime.root / "tracks" / self.track
                and local_path(module.PAID_REQUEST_JOURNAL_DIR) == self.root / "data/provider_request_journal",
                "experimental audit runtime/root mismatch")
        self.files = {runtime.root / "plan.json": file_sha(runtime.root / "plan.json"),
                      runtime.root / "budget.json": file_sha(runtime.root / "budget.json")}
        plan = validate_root(runtime.root)
        self.model = ModelPolicy.from_dict(plan["model"])
        self.plan_sha = plan["plan_sha256"]
        require(runtime.plan_sha256 == self.plan_sha and runtime.model == self.model
                and module.MODEL == self.model.provider_model, "experimental audit recorded model policy mismatch")
        expected = accounting_identity(self.model, self.plan_sha)
        require(all(module.progress.get(k) == v for k, v in expected.items()),
                "experimental audit accounting/provider policy mismatch")
        budget = ExperimentBudget(runtime.root, plan_sha256=self.plan_sha,
                                  limits=plan["limits"], model=self.model)
        require(runtime.budget.identity == budget.identity and runtime.budget.path == budget.path
                and runtime.budget.model == self.model,
                "experimental audit runtime/budget mismatch")
        self.ledger = budget.load()
        self.seen = set()
        self.previous = {}
        self.closed = set()

    def start(self, checkpoint, initial):
        self.checkpoint = checkpoint
        self.nodes = [c["chunk_id"] for c in initial]
        self.last = {r["chunk_id"]: i for i, r in enumerate(checkpoint["attempt_records"])}
        self.position = 0

    def accept(self, attempt, kwargs):
        module, model = self.module, self.model
        chunk_id, kind = attempt["chunk_id"], attempt["kind"]
        operation, helper = KINDS[kind]
        metadata = attempt["request_metadata"]
        fields = {"chunk_id", "chunk_index", "total_chunks", "audit_window", "candidate_ids",
                  "model_candidate_ids", "model_id_transport_version", "family_policy",
                  "family_context", "candidate_set_hash"}
        if kind == "semantic_correction":
            fields |= {"operation_id", "candidate_sources", "authorized_fields", "allowed_merge_targets"}
            require(metadata.get("operation_id") == attempt["operation_id"],
                    "experimental correction metadata operation drift")
        require(set(metadata) == fields and metadata["chunk_id"] == chunk_id,
                "experimental audit metadata schema/identity drift")
        require(chunk_id in self.nodes and chunk_id not in self.closed
                and next(n for n in self.nodes if n not in self.closed) == chunk_id,
                "experimental audit attempt order drift")
        require(metadata.get("chunk_index") == self.nodes.index(chunk_id) + 1
                and metadata.get("total_chunks") == len(self.nodes)
                and metadata.get("family_policy") == audit.AUDIT_FAMILY_POLICY_VERSION
                and attempt.get("lifecycle_version") == audit.AUDIT_ATTEMPT_LIFECYCLE_VERSION,
                "experimental audit source/chunk metadata drift")
        previous = self.previous.get(chunk_id)
        if kind == "audit":
            require(attempt["attempt"] == (previous["attempt"] + 1 if previous else 1)
                    and (previous is None or previous["kind"] == "audit"),
                    "experimental audit attempt sequence drift")
        elif kind == "json_repair":
            require(previous and previous["kind"] == "audit"
                    and previous["attempt"] == attempt["attempt"]
                    and previous["finish_reason"] == "stop", "experimental JSON repair lineage drift")
            try:
                json.loads(module.clean_json_text(previous["raw_content"]))
            except ValueError:
                pass
            else:
                require(False, "experimental JSON repair original is parseable")
        else:
            require(previous and previous["kind"] in {"audit", "json_repair"},
                    "experimental semantic correction lineage drift")
        require(attempt["operation_id"] not in self.seen, "duplicate experimental audit operation")
        self.seen.add(attempt["operation_id"])
        wire = normalize_request(model, operation, kwargs)
        chapter = self.checkpoint["audit_window"]["end_chapter"]
        identity = request_identity(operation_kind=operation, novel_id=self.track, chapter=chapter,
            provider=model.provider, model=model.provider_model, profile_id=module.ACTIVE_PROFILE.id,
            source_sha256=text_sha(module.chapter_map[chapter]["text"]), request_payload=wire)
        identity.update(experiment_plan_sha256=self.plan_sha, model_policy_sha256=model.sha256)
        identity["operation_id"] = digest({k: v for k, v in identity.items() if k != "operation_id"})
        scope = "audit_" + text_sha(helper + ":" + chunk_id)
        path = local_path(self.root / "data/provider_request_journal" /
                          f"{chapter:04d}.{operation}.{scope}.context0000.json")
        self.files[path] = file_sha(path)
        journal = load_matching_journal(path, identity)
        require(journal is not None and journal["status"] == "response_recorded",
                "experimental audit journal lacks RESPONSE_RECORDED")
        require(not ({"provider_policy", "wire_request_sha256", "auxiliary_domain_binding"} & journal.keys())
                and journal.get("attempt_context") == {"plan_sha256": self.plan_sha},
                "foreign provider evidence is not experimental audit evidence")
        reservation = self.ledger["records"].get(identity["operation_id"])
        prompt_upper = len(canonical(wire).encode("utf-8")) + 8192
        require(reservation is not None and reservation == dict(
            operation_id=identity["operation_id"], request_sha256=identity["request_sha256"],
            journal_path=str(path), track=self.track, chapter=chapter, status="accepted",
            prompt_upper=prompt_upper, reserved_ce=str(model.charge(prompt_upper, wire["max_completion_tokens"])),
            actual_ce=reservation.get("actual_ce"), response_sha256=journal["response_sha256"]),
            "experimental audit durable settlement identity drift")
        envelope = accept_response(model, journal["response"], wire, prompt_upper=prompt_upper)
        require(reservation["actual_ce"] == envelope["stats"]["cost_credit_equivalent"],
                "experimental audit settlement/accounting drift")
        envelope["stats"]["request_started_utc"] = journal["created_utc"]
        recorded = dict(content=attempt.get("raw_content"), finish_reason=attempt.get("finish_reason"),
            stats=attempt.get("stats"), provider_response_model=journal["response"]["model"],
            provider_response_id=journal["response"].get("id"))
        require(digest(recorded) == digest(envelope), "experimental accepted envelope/accounting hash drift")
        self.previous[chunk_id] = attempt
        if attempt.get("lifecycle") == "validated":
            self.closed.add(chunk_id)
        chunk = next(c for c in self.checkpoint["chunks"] if c["chunk_id"] == chunk_id)
        if self.position == self.last[chunk_id] and chunk["status"] == "split":
            require(attempt["finish_reason"] == "length", "experimental split lacks truncated response")
            children = audit._split_chunk(chunk)
            index = self.nodes.index(chunk_id)
            self.nodes[index:index + 1] = [c["chunk_id"] for c in children]
        self.position += 1
        return identity, path, journal, envelope

    def finish(self, records):
        require(all(n in self.closed for n in self.nodes), "experimental audit lacks terminal leaf proof")
        require(all(file_sha(p) == sha for p, sha in self.files.items()),
                "experimental audit evidence changed during proof")
        stats = {f: sum(r["stats"].get(f, 0) for r in records) for f in audit.STAT_TOTAL_FIELDS}
        ce = sum((decimal(r["stats"]["cost_credit_equivalent"]) for r in records), decimal("0"))
        stats.update({k: v for k, v in accounting_identity(self.model, self.plan_sha).items()
                      if k != "experimental_plan_sha256"})
        stats.update(cost_credit_equivalent=str(ce), cost_usd=float(ce), rate_correct_cost_usd=float(ce))
        require(stats == self.module.combine_stats(*(r["stats"] for r in records)),
                "experimental installed aggregate/accounting policy drift")
        stats["api_request_count"] = len(records)
        return stats
