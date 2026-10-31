"""Term changes you ask for: "change X to Y". The next audit applies them through its normal correction path.

data/requested_corrections.jsonl holds what's waiting. Once an audit starts, its
requests move to data/requested_corrections/<start>_<end>.json and never change,
so re-checking that audit later gives the same result.
"""
import json
from pathlib import Path

from noveltranslator_core import atomic_write_json

QUEUE_FILE = "requested_corrections.jsonl"
WINDOW_DIR = "requested_corrections"


def add_request(data_dir, *, chinese, new_english):
    chinese, new_english = chinese.strip(), new_english.strip()
    if not chinese or not new_english:
        raise ValueError("a correction needs the source term and the new English")
    with (Path(data_dir) / QUEUE_FILE).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"chinese": chinese, "new_english": new_english}, ensure_ascii=False) + "\n")


def waiting(data_dir):
    path = Path(data_dir) / QUEUE_FILE
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _window_path(data_dir, start, end):
    return Path(data_dir) / WINDOW_DIR / f"{start:04d}_{end:04d}.json"


def claim_for_audit(data_dir, start, end):
    """Move waiting requests to this audit. Safe to repeat after a crash."""
    path = _window_path(data_dir, start, end)
    requests = waiting(data_dir)
    if path.is_file() or not requests:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, requests)
    (Path(data_dir) / QUEUE_FILE).unlink()


def for_audit(data_dir, start, end):
    path = _window_path(data_dir, start, end)
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
