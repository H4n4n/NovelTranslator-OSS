#!/usr/bin/env python3
"""Fail-closed publication guard.

Modes:
  --index (default)  validate the staged index against the staged inventory
  --tree REV         validate a commit tree against that commit's inventory
  --push-check       validate all local branches, tags and HEAD (detached-safe)
  --pre-push         consume Git pre-push stdin ref updates and validate the
                     newly reachable outgoing commits, trees and annotated tags

Inventories are read from the snapshot being checked (index blob or commit),
never from an unstaged working-tree file. Any Git failure, missing object,
unmerged entry, unsupported mode, prohibited path, invalid UTF-8, binary
content or credential pattern fails closed. Diagnostics never print matched
secret values.

This is one defense, not proof of confidentiality. Hooks are opt-in per clone,
can be removed/modified, and can be bypassed with --no-verify. CI runs after
upload and cannot prevent initial disclosure. Security-critical changes to the
guard, hook, inventory or workflow require trusted human review.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys

INVENTORY_NAME = "public_inventory.txt"
ALLOWED_MODES = {"100644", "100755"}

FORBIDDEN_COMPONENTS = {
    "novels", "data", "backup", "backups", "incoming", "output", "outputs",
    "final", "pending", "journal", "journals", "ledgers", "ledger",
    ".commandcode", ".git", "_evidence", "_scratch", "reports", "local_data",
    "secrets", "secret", ".ssh",
}

SECRET_PATTERNS = {
    "provider_key": re.compile(r"\b(?:sk|ds)-[A-Za-z0-9_-]{18,}\b"),
    "github_token": re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{25,}\b|github_pat_[A-Za-z0-9_]{20,}"),
    "aws_key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "private_key": re.compile("-----BEGIN " + r"(?:RSA |EC |OPENSSH |DSA |ENCRYPTED |PGP )?PRIVATE KEY-----"),
    "password_literal": re.compile(r"(?i)\b(?:password|passwd|pwd|secret|token|api[_-]?key|client[_-]?secret)[ \t]{0,8}[:=][ \t]{0,4}[\"'][^\"'\r\n]{8,}[\"']"),
    "hex_secret": re.compile(r"(?i)\b(?:api[_-]?key|secret[_-]?key|access[_-]?key|private[_-]?key|client[_-]?secret|token|password|passwd|pwd)\s*[:=]\s*[\"'][0-9a-f]{16,}[\"']"),
    "bearer": re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._-]{24,}"),
    "long_base64": re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{120,}={0,2}(?![A-Za-z0-9+/=])"),
}
BASE64_CANDIDATE = re.compile(r"(?<![A-Za-z0-9+/=])([A-Za-z0-9+/]{16,}={0,2})(?![A-Za-z0-9+/=])")
SECRET_ASSIGNMENT = re.compile(
    r"(?im)^[ \t]*(?:api[_-]?key|secret[_-]?key|access[_-]?key|private[_-]?key|"
    r"client[_-]?secret|secret|token|password|passwd|pwd)[ \t]{0,8}[:=][ \t]{0,8}"
)
QUOTED_FRAGMENT = re.compile(r"[\"']([^\"'\r\n]*)[\"']")

try:
    from guard_allowlist import EXACT_SYNTHETIC
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from guard_allowlist import EXACT_SYNTHETIC


class GuardError(Exception):
    pass


REPO = None


def git(args, input_bytes=None, timeout=180):
    proc = subprocess.run(["git", "-C", REPO, *args], input=input_bytes, capture_output=True, timeout=timeout)
    if proc.returncode != 0:
        raise GuardError("git command failed: " + " ".join(args))
    return proc.stdout


def repo_root():
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for candidate in (base, os.getcwd()):
        proc = subprocess.run(["git", "-C", candidate, "rev-parse", "--show-toplevel"], capture_output=True)
        if proc.returncode == 0:
            return proc.stdout.decode("utf-8", errors="strict").strip()
    raise GuardError("not a git repository")


def parse_inventory(raw, label):
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise GuardError("inventory is not valid UTF-8: " + label)
    entries = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        entries.append(line)
    if not entries:
        raise GuardError("empty reviewed inventory: " + label)
    if len(set(entries)) != len(entries):
        raise GuardError("duplicate entries in reviewed inventory: " + label)
    for entry in entries:
        check_path_shape(entry)
    return set(entries)


def inventory_from_index():
    return parse_inventory(git(["show", ":" + INVENTORY_NAME]), "index")


def inventory_from_commit(commit):
    return parse_inventory(git(["show", commit + ":" + INVENTORY_NAME]), commit[:12])


def check_path_shape(path):
    parts = path.replace("\\", "/").split("/")
    if any(part == "" or part in (".", "..") for part in parts):
        raise GuardError("empty or traversal path component: " + path)
    for part in parts[:-1]:
        if part.casefold() in FORBIDDEN_COMPONENTS:
            raise GuardError("forbidden directory component in " + path)
    name = parts[-1].casefold()
    if name in FORBIDDEN_COMPONENTS or name.startswith(".env"):
        raise GuardError("forbidden file name: " + path)


def check_path(path, inventory):
    check_path_shape(path)
    if path not in inventory:
        raise GuardError("unexpected path not in reviewed inventory: " + path)


def _allow_match(path, match):
    digest = hashlib.sha256(match.group(0).encode("utf-8")).hexdigest()
    return digest in EXACT_SYNTHETIC.get(path, set())


def _scan_split_literals(path, text):
    for match in SECRET_ASSIGNMENT.finditer(text):
        window = text[match.end():match.end() + 400]
        parts = QUOTED_FRAGMENT.findall(window)
        if len(parts) < 2:
            continue
        joined = "".join(parts)
        for name, pattern in SECRET_PATTERNS.items():
            hit = pattern.search(joined)
            if hit and not _allow_match(path, hit):
                raise GuardError("split secret pattern '" + name + "' in " + path)


def scan_content(path, data):
    if b"\x00" in data:
        raise GuardError("binary content: " + path)
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise GuardError("invalid UTF-8: " + path)
    for name, pattern in SECRET_PATTERNS.items():
        for match in pattern.finditer(text):
            if _allow_match(path, match):
                continue
            raise GuardError("secret pattern '" + name + "' in " + path)
    for match in BASE64_CANDIDATE.finditer(text):
        candidate = match.group(1)
        try:
            padded = candidate + "=" * (-len(candidate) % 4)
            decoded = base64.b64decode(padded, validate=True).decode("utf-8", errors="ignore")
        except Exception:
            continue
        for name, pattern in SECRET_PATTERNS.items():
            if pattern.search(decoded):
                raise GuardError("encoded secret pattern '" + name + "' in " + path)
    _scan_split_literals(path, text)


def parse_z(blob):
    return [record for record in blob.split(b"\x00") if record]


def validate_index():
    out = git(["ls-files", "--stage", "-z"])
    records = parse_z(out)
    if not records:
        raise GuardError("empty index")
    inventory = inventory_from_index()
    paths = set()
    for record in records:
        meta, raw_path = record.split(b"\t", 1)
        parts = meta.decode("ascii").split()
        if len(parts) != 3:
            raise GuardError("malformed index entry")
        mode, sha, stage = parts
        try:
            path = raw_path.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise GuardError("invalid UTF-8 index path")
        if stage != "0":
            raise GuardError("unmerged index entry: " + path)
        if mode not in ALLOWED_MODES:
            raise GuardError("unsupported index mode " + mode + ": " + path)
        if path in paths:
            raise GuardError("duplicate index path: " + path)
        paths.add(path)
        check_path(path, inventory)
        scan_content(path, git(["cat-file", "blob", sha]))
    if paths != inventory:
        raise GuardError("inventory/index mismatch (" + str(len(inventory - paths)) + " inventory entries not staged)")
    return len(paths)


def _is_shallow():
    return git(["rev-parse", "--is-shallow-repository"]).decode("ascii", "strict").strip() == "true"


def _object_type(sha):
    return git(["cat-file", "-t", sha]).decode("ascii", "strict").strip()


def _commit_for(sha):
    return git(["rev-parse", sha + "^{commit}"]).decode("ascii", "strict").strip()


def validate_commit(commit, seen=None):
    seen = seen if seen is not None else set()
    resolved = _commit_for(commit)
    if resolved in seen:
        return 0
    seen.add(resolved)
    inventory = inventory_from_commit(resolved)
    out = git(["ls-tree", "-r", "-l", "-z", resolved])
    count = 0
    for record in parse_z(out):
        meta, raw_path = record.split(b"\t", 1)
        parts = meta.decode("ascii").split()
        if len(parts) != 4:
            raise GuardError("malformed tree entry in " + resolved)
        mode, kind, sha, _size = parts
        try:
            path = raw_path.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise GuardError("invalid UTF-8 tree path in " + resolved)
        if kind != "blob":
            raise GuardError("non-blob tree entry (" + kind + "): " + path)
        if mode not in ALLOWED_MODES:
            raise GuardError("unsupported tree mode " + mode + ": " + path)
        check_path(path, inventory)
        scan_content(path, git(["cat-file", "blob", sha]))
        count += 1
    message = git(["log", "-1", "--format=%B", resolved])
    if message.strip():
        scan_content("<commit:" + resolved[:12] + ">", message)
    parents = git(["rev-list", "--parents", "-n", "1", resolved]).decode("ascii", "strict").split()[1:]
    for parent in parents:
        if subprocess.run(["git", "-C", REPO, "cat-file", "-e", parent + "^{commit}"],
                          capture_output=True).returncode != 0:
            raise GuardError("missing ancestry object for " + resolved)
    return count


def _tag_header(raw):
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise GuardError("annotated tag object is not valid UTF-8")
    header = {}
    for line in text.splitlines():
        if not line.strip():
            break
        key, separator, value = line.partition(" ")
        if separator and key in {"object", "type", "tag", "tagger"}:
            header[key] = value
    return header


def validate_object(sha, seen, seen_tags=None):
    seen_tags = seen_tags if seen_tags is not None else set()
    kind = _object_type(sha)
    if kind == "tag":
        if sha in seen_tags:
            raise GuardError("annotated tag cycle or repeated tag object")
        seen_tags.add(sha)
        raw = git(["cat-file", "tag", sha])
        scan_content("<tag:" + sha[:12] + ">", raw)
        header = _tag_header(raw)
        target = header.get("object")
        target_type = header.get("type")
        if not target or not target_type:
            raise GuardError("malformed annotated tag object")
        if target_type not in {"commit", "tag"}:
            raise GuardError("unsupported annotated tag target type: " + target_type)
        return validate_object(target, seen, seen_tags)
    if kind != "commit":
        raise GuardError("unsupported outgoing object type: " + kind)
    return validate_commit(sha, seen)


def validate_reachable(sha, seen, messages=True):
    commits = git(["rev-list", sha]).decode("ascii", "strict").split()
    if not commits:
        raise GuardError("no commits reachable from outgoing object")
    total = 0
    for commit in commits:
        total += validate_commit(commit, seen)
    return total


def pre_push_updates():
    raw = sys.stdin.buffer.read()
    if not raw.strip():
        return []
    updates = []
    for line in raw.decode("utf-8", errors="strict").splitlines():
        parts = line.split()
        if len(parts) != 4:
            raise GuardError("malformed pre-push input")
        updates.append(parts)
    return updates


def validate_pre_push(updates):
    if _is_shallow():
        raise GuardError("shallow repository: ancestry cannot be validated")
    seen = set()
    checked = 0
    saw_deletion = False
    for local_ref, local_sha, remote_ref, remote_sha in updates:
        if set(local_sha) == {"0"}:
            saw_deletion = True
            continue
        kind = _object_type(local_sha)
        checked += validate_object(local_sha, seen)
        if kind == "tag":
            peeled = _commit_for(local_sha)
            checked += validate_reachable(peeled, seen)
            continue
        if remote_sha and set(remote_sha) != {"0"}:
            if subprocess.run(["git", "-C", REPO, "cat-file", "-e", remote_sha + "^{commit}"],
                              capture_output=True).returncode == 0:
                for commit in git(["rev-list", remote_sha + ".." + local_sha]).decode("ascii", "strict").split():
                    checked += validate_commit(commit, seen)
                continue
        checked += validate_reachable(local_sha, seen)
    if checked == 0 and not saw_deletion:
        raise GuardError("no outgoing objects validated")
    return checked


def validate_push_check():
    if _is_shallow():
        raise GuardError("shallow repository: ancestry cannot be validated")
    seen = set()
    checked = 0
    refs = git(["for-each-ref", "--format=%(objectname)", "refs/heads", "refs/tags"]).decode("ascii", "strict").split()
    head = subprocess.run(["git", "-C", REPO, "rev-parse", "--verify", "HEAD"],
                          capture_output=True)
    if head.returncode == 0:
        refs.append(head.stdout.decode("ascii", "strict").strip())
    if not refs:
        raise GuardError("no local refs or HEAD to validate")
    for sha in dict.fromkeys(refs):
        kind = _object_type(sha)
        checked += validate_object(sha, seen)
        peeled = _commit_for(sha) if kind == "tag" else sha
        checked += validate_reachable(peeled, seen)
    if checked == 0:
        raise GuardError("no objects validated")
    return checked


def main(argv=None):
    parser = argparse.ArgumentParser(description="Fail-closed publication guard")
    parser.add_argument("--index", "--staged", dest="index", action="store_true")
    parser.add_argument("--tree", metavar="REV")
    parser.add_argument("--push-check", action="store_true")
    parser.add_argument("--pre-push", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    result = {"mode": "index", "ok": False, "checked": 0, "error": ""}
    try:
        if args.pre_push:
            result["mode"] = "pre-push"
            updates = pre_push_updates()
            result.update(ok=True, checked=validate_pre_push(updates) if updates else validate_push_check())
        elif args.push_check:
            result["mode"] = "push-check"
            result.update(ok=True, checked=validate_push_check())
        elif args.tree:
            result["mode"] = "tree"
            result.update(ok=True, checked=validate_commit(args.tree))
        else:
            result.update(ok=True, checked=validate_index())
    except GuardError as exc:
        result["error"] = str(exc)
    except subprocess.TimeoutExpired:
        result["error"] = "git command timed out"
    except Exception as exc:
        result["error"] = "unexpected guard failure: " + type(exc).__name__

    if args.json:
        print(json.dumps(result))
    else:
        print(("publication_guard: PASS (%s, files=%d)" if result["ok"] else "publication_guard: FAIL (%s): %s")
              % ((result["mode"], result["checked"]) if result["ok"] else (result["mode"], result["error"])))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    try:
        REPO = repo_root()
    except GuardError as exc:
        print("publication_guard: FAIL (repository): " + str(exc))
        raise SystemExit(1)
    raise SystemExit(main())
