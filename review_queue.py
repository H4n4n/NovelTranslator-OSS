"""Items a run flagged and kept going past: data/review_queue.jsonl in each novel.

Append-only and advisory. A failure here never stops translation.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

QUEUE_FILE = "review_queue.jsonl"

SOFT_COMMIT_BLOCKERS = frozenset({"residual_cjk", "residual_source_script", "locked_terminology"})


def add_item(data_dir, *, chapter, kind, detail=""):
    path = Path(data_dir) / QUEUE_FILE
    entry = {"chapter": chapter, "kind": kind, "detail": str(detail)[:500],
             "utc": datetime.now(timezone.utc).isoformat()}
    try:
        if any(item["chapter"] == chapter and item["kind"] == kind for item in read_items(data_dir)):
            return
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def read_items(data_dir):
    path = Path(data_dir) / QUEUE_FILE
    if not path.is_file():
        return []
    items = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            items.append(json.loads(line))
        except ValueError:
            continue
    return items


def soften_commit_blockers(qa):
    """Turn repairable reader-visible flaws into flags, so the chapter can commit.

    Only leftover source script and term mismatches qualify. Empty, truncated or
    incomplete output stays a hard failure, because those lose story content.
    """
    blockers = qa.get("commit_blockers", [])
    if qa.get("commit_allowed") or not blockers:
        return qa
    if not all(item.get("type") in SOFT_COMMIT_BLOCKERS for item in blockers):
        return qa
    return {**qa, "commit_allowed": True, "commit_blockers": [], "flagged_blockers": blockers}
