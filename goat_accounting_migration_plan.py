"""Read-only direct-to-GOAT accounting epoch plan. No client or secret access."""
import argparse
import hashlib
import json
import subprocess
from pathlib import Path

from novel_project import NOVEL_ID_RE, load_novel_project, novels_root_for
from production_accounting import (
    TOTAL_FIELDS, PROVIDER_COMMAND_CODE, MIGRATION_REVISION, MIGRATION_POLICY_SHA256,
    canonical_sha256, migrate_direct_progress, validate_progress_accounting,
)
from production_paid_request_journal import inspect_paid_request_journals

READY = "READY_DIRECT_TO_GOAT_ACCOUNTING_MIGRATION"


def sha256(content):
    return hashlib.sha256(content).hexdigest()


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                       allow_nan=False) + "\n").encode("utf-8")


def read_object(content):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key: " + key)
            result[key] = value
        return result
    value = json.loads(content.decode("utf-8-sig"), object_pairs_hook=pairs,
        parse_constant=lambda v: (_ for _ in ()).throw(ValueError("nonfinite JSON: " + v)))
    if not isinstance(value, dict):
        raise ValueError("expected JSON object")
    return value


def require_local_path(path, root):
    """Reject links/junction escapes before reading or writing selected state."""
    path, root = Path(path), Path(root).resolve()
    if path.resolve() != root and root not in path.resolve().parents:
        raise ValueError("path escapes novel root")
    for part in (path, *path.parents):
        if part == root:
            break
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise ValueError("linked novel state forbidden")
    return path


def inventory(root, novel_root):
    require_local_path(root, novel_root)
    if not root.exists():
        return []
    if not root.is_dir():
        raise ValueError("inventory root is not a directory")
    result = []
    for path in sorted(root.rglob("*")):
        require_local_path(path, novel_root)
        if path.is_dir():
            result.append(dict(path=str(path.relative_to(novel_root)), kind="directory"))
        elif path.is_file():
            if path.name == ".env" or path.suffix == ".dpapi":
                raise ValueError("unexpected credential artifact in inventory")
            content = path.read_bytes()
            result.append(dict(path=str(path.relative_to(novel_root)), kind="file",
                               sha256=sha256(content), bytes=len(content)))
        else:
            raise ValueError("nonregular inventory entry")
    return result


def _head(root):
    result = subprocess.run(["git", "-c", "safe.directory=" + str(root), "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=False)
    if result.returncode != 0 and (Path(root) / ".git").exists():
        raise ValueError("cannot bind repository HEAD")
    return result.stdout.strip() if result.returncode == 0 else None


def code_identity():
    root = Path(__file__).resolve().parent
    files = sorted(root.glob("*.py")) + [root / "pyproject.toml"]
    files += sorted(p for p in (root / "profiles").rglob("*")
                    if p.suffix in {".json", ".txt", ".md"} and not p.name.startswith(".env"))
    hashes = {str(p.relative_to(root)): sha256(p.read_bytes()) for p in files if p.is_file()}
    return dict(head=_head(root), files=hashes, code_tree_sha256=canonical_sha256(hashes))


def external_path(path, repo_root):
    path, root = Path(path).resolve(), Path(repo_root).resolve()
    if path == root or root in path.parents:
        raise ValueError("evidence/output must be external to repository")
    return path


def build_goat_accounting_migration_plan(novel_id, *, repo_root):
    if not isinstance(novel_id, str) or not NOVEL_ID_RE.fullmatch(novel_id):
        raise ValueError("invalid novel ID")
    repo = Path(repo_root).resolve()
    root = novels_root_for(repo) / novel_id
    require_local_path(root, root.parent)
    config_path = require_local_path(root / "novel.json", root)
    progress_path = require_local_path(root / "data/progress.json", root)
    config_bytes, before = config_path.read_bytes(), progress_path.read_bytes()
    config = read_object(config_bytes)
    if config.get("id") != novel_id:
        raise ValueError("novel identity mismatch")
    # Validate configuration without runtime initialization, profile state, or credentials.
    load_novel_project(novel_id, repo_root=repo)
    pending = inventory(root / "output/pending", root)
    journal_inventory = inventory(root / "data/provider_request_journal", root)
    journals = inspect_paid_request_journals(root / "data/provider_request_journal")
    temps = sorted(p.name for p in progress_path.parent.iterdir()
                   if p.name.endswith(".tmp") and "progress.json" in p.name)
    state = dict(repo_root=str(repo), repository_head=_head(repo), novel_id=novel_id,
        novel_json=config, novel_json_sha256=sha256(config_bytes),
        progress_before_sha256=sha256(before), code_identity=code_identity(),
        pending_inventory=pending, paid_journal_inventory=journal_inventory,
        journal_inspection=journals, progress_temps=temps,
        migration_revision=MIGRATION_REVISION, migration_policy_sha256=MIGRATION_POLICY_SHA256)
    proposed, progress, error = None, None, None
    status = "BLOCKED_INVALID_PROGRESS"
    try:
        progress = read_object(before)
        provider = validate_progress_accounting(progress)
        # Boundary validation is mandatory even for classifications that need no migration.
        from production_accounting import _boundary
        _boundary(progress)
        if journals["uncertain"] or journals["errors"]:
            status = "BLOCKED_AMBIGUOUS_PAID_STATE"
        elif journals["records"] or journal_inventory:
            status = "BLOCKED_EXISTING_PAID_JOURNAL"
        elif pending:
            status = "BLOCKED_PENDING_PAID_WORK"
        elif temps:
            status = "BLOCKED_INVALID_PROGRESS"
            error = "interrupted progress temp requires independent review"
        elif provider == PROVIDER_COMMAND_CODE:
            status = "ALREADY_GOAT_ACTIVE"
        elif not any(progress.get(k, 0) for k in TOTAL_FIELDS):
            status = "ZERO_DIRECT_STATE_NO_MIGRATION_REQUIRED"
        else:
            proposed = migrate_direct_progress(progress, sha256(before))
            status = READY
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        error = str(exc)
        if progress is not None:
            status = "BLOCKED_UNRECOGNIZED_ACCOUNTING_STATE"
    state["progress"] = progress
    plan = dict(status=status, error=error, state_binding=state,
        proposed_progress=proposed, api_calls=0, credential_reads=0, project_writes=0,
        paid_execution_authorized=False,
        started_uncertain_count=len(journals["uncertain"]))
    if proposed is not None:
        plan.update(proposed_progress_sha256=sha256(json_bytes(proposed)),
                    proposed_progress_canonical_sha256=canonical_sha256(proposed))
    plan["state_fingerprint"] = canonical_sha256(state)
    plan["approval_token"] = canonical_sha256(plan)
    plan["plan_sha256"] = canonical_sha256(plan)
    # Re-read bindings: do not issue approval over a torn snapshot.
    if (config_path.read_bytes() != config_bytes or progress_path.read_bytes() != before
        or inventory(root / "output/pending", root) != pending
        or inventory(root / "data/provider_request_journal", root) != journal_inventory):
        raise ValueError("state drift during planning")
    return plan


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--novel", required=True)
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    plan = build_goat_accounting_migration_plan(args.novel, repo_root=args.repo_root)
    if args.output:
        output = external_path(args.output, args.repo_root)
        # Exclusive external publication: never clobber existing evidence.
        with output.open("xb") as handle:
            handle.write(json_bytes(plan))
    print(json_bytes(plan).decode("utf-8"))
    return 0 if plan["status"] in {READY, "ALREADY_GOAT_ACTIVE", "ZERO_DIRECT_STATE_NO_MIGRATION_REQUIRED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
