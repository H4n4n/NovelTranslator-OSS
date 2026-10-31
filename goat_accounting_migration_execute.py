"""Explicit local-only accounting migration executor. No inference/credential path."""
import argparse
import os
from pathlib import Path

from goat_accounting_migration_plan import (
    READY, build_goat_accounting_migration_plan, external_path, json_bytes,
    read_object, require_local_path, sha256,
)
from noveltranslator_core import atomic_write_bytes
from production_accounting import canonical_sha256, migrate_direct_progress, validate_progress_accounting
from production_run_lock import LOCK_NAME, production_run_lock


def _evidence(path, content):
    """Exclusive, fsynced evidence. Exact existing evidence is restart-safe."""
    if path.exists():
        if path.is_symlink() or path.read_bytes() != content:
            raise ValueError("conflicting external evidence: " + str(path))
        return
    with path.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    if path.read_bytes() != content:
        raise ValueError("external evidence readback failed")


def execute_goat_accounting_migration(novel_id, *, repo_root, plan,
        state_fingerprint, approval_token, evidence_root, apply_accounting_migration=False):
    if not apply_accounting_migration:
        return dict(status="NOT_APPLIED", project_writes=0, api_calls=0)
    if not isinstance(plan, dict) or plan.get("status") != READY:
        raise ValueError("migration requires eligible READY plan")
    if (plan.get("state_fingerprint") != state_fingerprint
        or plan.get("approval_token") != approval_token
        or plan.get("plan_sha256") != canonical_sha256({k: v for k, v in plan.items() if k != "plan_sha256"})
        or approval_token != canonical_sha256({k: v for k, v in plan.items() if k not in {"approval_token", "plan_sha256"}})
        or state_fingerprint != canonical_sha256(plan["state_binding"])):
        raise ValueError("plan/fingerprint/approval mismatch")
    expected = migrate_direct_progress(plan["state_binding"]["progress"],
                                      plan["state_binding"]["progress_before_sha256"])
    after = json_bytes(expected)
    if (canonical_sha256(expected) != canonical_sha256(plan["proposed_progress"])
        or sha256(after) != plan["proposed_progress_sha256"]
        or canonical_sha256(expected) != plan["proposed_progress_canonical_sha256"]):
        raise ValueError("proposed migration mismatch")
    repo = Path(repo_root).resolve()
    # Resolve caller identity before acquiring a lock (no arbitrary path from a plan).
    from novel_project import NOVEL_ID_RE, novels_root_for
    if not NOVEL_ID_RE.fullmatch(novel_id):
        raise ValueError("invalid novel ID")
    novels_root = novels_root_for(repo)
    root = require_local_path(novels_root / novel_id, novels_root)
    if plan["state_binding"]["novel_id"] != novel_id or plan["state_binding"]["repo_root"] != str(repo):
        raise ValueError("novel/repository identity mismatch")
    evidence = external_path(evidence_root, repo)
    # The production lock opens a persistent rendezvous file. Require it to exist
    # so this tool's sole persistent novel change remains progress.json.
    lock = require_local_path(root / LOCK_NAME, root)
    if not lock.is_file() or lock.stat().st_size != 0:
        raise ValueError("existing empty production lock rendezvous required")
    progress_path = require_local_path(root / "data/progress.json", root)
    with production_run_lock(root):
        current = build_goat_accounting_migration_plan(novel_id, repo_root=repo)
        before = progress_path.read_bytes()
        already = before == after
        if already:
            validate_progress_accounting(read_object(before))
            if current["status"] != "ALREADY_GOAT_ACTIVE":
                raise ValueError("already-applied state now blocked")
            binding = dict(current["state_binding"])
            binding["progress"] = plan["state_binding"]["progress"]
            binding["progress_before_sha256"] = plan["state_binding"]["progress_before_sha256"]
            if canonical_sha256(binding) != state_fingerprint:
                raise ValueError("already-applied state bindings drifted")
        elif canonical_sha256(current) != canonical_sha256(plan):
            raise ValueError("stale plan/current state mismatch")
        # Save original bytes BEFORE replacement. Replay requires that same backup.
        evidence.mkdir(parents=True, exist_ok=True)
        directory = require_local_path(evidence / state_fingerprint, evidence)
        directory.mkdir(exist_ok=True)
        backup = directory / "progress.before.json"
        if already:
            if not backup.is_file() or sha256(backup.read_bytes()) != plan["state_binding"]["progress_before_sha256"]:
                raise ValueError("already-applied migration lacks exact original evidence")
        else:
            _evidence(backup, before)
        _evidence(directory / "plan.json", json_bytes(plan))
        if not already:
            # Recheck after external persistence, immediately before the sole write.
            fresh = build_goat_accounting_migration_plan(novel_id, repo_root=repo)
            if canonical_sha256(fresh) != canonical_sha256(plan) or sha256(before) != plan["state_binding"]["progress_before_sha256"]:
                raise ValueError("state drift before replacement")
            try:
                atomic_write_bytes(progress_path, after)
            except OSError:
                # An observable completed replace is committed; never rollback it.
                if progress_path.read_bytes() != after:
                    raise
        actual = progress_path.read_bytes()
        validate_progress_accounting(read_object(actual))
        if actual != after:
            raise ValueError("migration post-write readback mismatch; preserve state for review")
        receipt = dict(status="APPLIED", novel_id=novel_id,
            progress_before_sha256=plan["state_binding"]["progress_before_sha256"],
            progress_after_sha256=sha256(actual), plan_sha256=plan["plan_sha256"],
            state_fingerprint=state_fingerprint, approval_token=approval_token,
            migration_revision=plan["state_binding"]["migration_revision"],
            migration_policy_sha256=plan["state_binding"]["migration_policy_sha256"],
            historical_epoch_payload_sha256=expected["accounting_history"][0]["epoch_payload_sha256"],
            api_calls=0, credential_reads=0, paid_execution_authorized=False)
        _evidence(directory / "receipt.json", json_bytes(receipt))
        return dict(receipt, status="ALREADY_APPLIED" if already else "APPLIED",
                    project_writes=0 if already else 1, evidence_directory=str(directory))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("novel", "repo-root", "plan", "state-fingerprint", "approval-token", "evidence-root"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--apply-accounting-migration", action="store_true")
    args = parser.parse_args(argv)
    result = execute_goat_accounting_migration(args.novel, repo_root=args.repo_root,
        plan=read_object(Path(args.plan).read_bytes()), state_fingerprint=args.state_fingerprint,
        approval_token=args.approval_token, evidence_root=args.evidence_root,
        apply_accounting_migration=args.apply_accounting_migration)
    print(json_bytes(result).decode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
