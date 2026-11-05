"""Compare a batch run with a sequential run of the same chapters (docs/BATCH_MODE.md).

prepare: copy one rehearsal novel into <out>/seq/novels/<id> and <out>/batch/novels/<id>
         (batch mode on), with a baseline. No provider calls. Each copy keeps the novel's
         id, which its audit history is bound to. Run each with translate.py --start
         --trial, with NOVELTRANSLATOR_NOVELS_ROOT set to that copy's novels folder.
compare: term consistency on terms first seen in the range, cost, wall time, review
         flags, and a list of chapters to read for story-state errors.

Only for rehearsal copies; it refuses a novels root that is not a rehearsal root.
"""
import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from novel_project import load_novel_config

SKIP_COPY = {"logs", ".production.lock", "trial_stop.json", "last_run.json", "run_budget.json"}
ARMS = ("seq", "batch")
TERMS_ARM = "terms"  # Terms-only batch: reads the source and settles terms, never translates.


def _read(path, default=None):
    path = Path(path)
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default


def _require_rehearsal(novels_root):
    if "rehearsal" not in [part.lower() for part in Path(novels_root).resolve().parts]:
        raise SystemExit(f"Refusing a novels root outside a rehearsal folder: {novels_root}")


def arm_root(out, arm, novel_id):
    return Path(out) / arm / "novels" / novel_id


def prepare(novels_root, novel_id, out, chapters, max_parallel=32, arms=ARMS):
    _require_rehearsal(novels_root)
    _require_rehearsal(out)
    source = Path(novels_root) / novel_id
    progress = _read(source / "data" / "progress.json")
    if list((source / "output" / "pending").glob("*.md")):
        raise SystemExit("The source novel has a pending translation; finish or recover it first.")
    start = progress["next_chapter"]
    for arm in arms:
        target = arm_root(out, arm, novel_id)
        if target.exists():
            raise SystemExit(f"{target} already exists")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target, ignore=lambda folder, names: [name for name in names if name in SKIP_COPY])
        config = json.loads((target / "novel.json").read_text(encoding="utf-8"))
        config.pop("batch_mode", None)
        if arm in ("batch", TERMS_ARM):
            config["batch_mode"] = {"enabled": True, "chapters": chapters, "max_parallel": max_parallel,
                                    "terms_only": arm == TERMS_ARM}
        (target / "novel.json").write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        load_novel_config(target / "novel.json")
        baseline = {"source_novel": novel_id, "arm": arm, "start_chapter": start, "end_chapter": start + chapters - 1,
                    "baseline_cost_usd": float(progress.get("rate_correct_cost_usd", 0.0)),
                    "baseline_utc": datetime.now(timezone.utc).isoformat()}
        (target / "data" / "rehearsal.json").write_text(json.dumps(baseline, indent=2), encoding="utf-8")
    return {"start_chapter": start, "end_chapter": start + chapters - 1,
            "arms": {arm: str(arm_root(out, arm, novel_id)) for arm in arms}}


def renderings(entry):
    values = entry.get("approved_renderings") if entry.get("status") == "contextual" else [entry.get("english")]
    return [value for value in values or [] if value]


def term_consistency(novel_root, reference_glossary, start, end, sources):
    """Of the occurrences of terms first seen in the range, how many use the reference rendering."""
    new_terms = {key: entry for key, entry in reference_glossary.items()
                 if start <= int(entry.get("first_chapter") or 0) <= end and renderings(entry)}
    hits, total, misses = 0, 0, []
    for number in range(start, end + 1):
        path = Path(novel_root) / "output" / "chapters" / f"{number:04d}.md"
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8").casefold()
        for key, entry in new_terms.items():
            if number < int(entry["first_chapter"]) or key not in sources.get(number, ""):
                continue
            total += 1
            if any(value.casefold() in text for value in renderings(entry)):
                hits += 1
            else:
                misses.append({"chapter": number, "source": key, "expected": renderings(entry)})
    return {"terms": len(new_terms), "uses": total, "hits": hits,
            "rate": round(hits / total, 4) if total else None, "misses": misses}


def _wall_seconds(novel_root, baseline_utc, end):
    started, finished = None, None
    for path in sorted((Path(novel_root) / "logs").glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            stamp = event.get("timestamp_utc", "")
            if stamp < baseline_utc:
                continue
            if event.get("event") == "program_start" and started is None:
                started = stamp
            if event.get("event") == "chapter_commit" and event.get("chapter") == end:
                finished = stamp
    if not started or not finished:
        return None
    return (datetime.fromisoformat(finished) - datetime.fromisoformat(started)).total_seconds()


def _flags(novel_root):
    counts = {}
    path = Path(novel_root) / "data" / "review_queue.jsonl"
    for line in (path.read_text(encoding="utf-8").splitlines() if path.is_file() else []):
        kind = json.loads(line).get("kind")
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def story_sample(batch_root, start, end, every=10):
    """Chapters to read side by side: every tenth, plus each chapter whose source notes record a lasting change."""
    chosen = {}
    sheets_dir = Path(batch_root) / "data" / "batch" / f"{start:04d}" / "sheets"
    for number in range(start, end + 1):
        sheet = _read(sheets_dir / f"{number:04d}.json") or {}
        lasting = [event["detail"] for event in sheet.get("events", [])
                   if event.get("type") in ("rename", "death", "title", "address", "move")]
        if lasting or (number - start) % every == every - 1:
            chosen[number] = lasting
    return chosen


def compare(out, novel_id):
    roots = {arm: arm_root(out, arm, novel_id) for arm in ARMS}
    baseline = _read(roots["seq"] / "data" / "rehearsal.json")
    start, end = baseline["start_chapter"], baseline["end_chapter"]
    sources = {item["chapter"]: item["text"] for item in _read(roots["seq"] / "data" / "chapters.json")
               if isinstance(item, dict) and "chapter" in item and "text" in item}
    glossaries = {arm: _read(root / "data" / "glossary.json")["entries"] for arm, root in roots.items()}
    report = {"novel": novel_id, "start_chapter": start, "end_chapter": end, "arms": {}}
    for arm, root in roots.items():
        progress = _read(root / "data" / "progress.json")
        arm_baseline = _read(root / "data" / "rehearsal.json")
        committed = min(progress["last_completed"], end) - start + 1
        cost = float(progress.get("rate_correct_cost_usd", 0.0)) - arm_baseline["baseline_cost_usd"]
        wall = _wall_seconds(root, arm_baseline["baseline_utc"], end)
        report["arms"][arm] = {
            "committed_chapters": max(committed, 0),
            "cost_usd": round(cost, 6), "cost_per_chapter_usd": round(cost / committed, 6) if committed > 0 else None,
            "wall_seconds": wall, "seconds_per_chapter": round(wall / committed, 1) if wall and committed > 0 else None,
            "own_term_consistency": term_consistency(root, glossaries[arm], start, end, sources),
            "review_flags": _flags(root)}
    report["batch_against_sequential_terms"] = term_consistency(roots["batch"], glossaries["seq"], start, end, sources)
    report["story_sample"] = story_sample(roots["batch"], start, end)
    return report


def term_selection(settled_terms, reference_glossary, start, end):
    """How a batch's settled terms compare with a sequential glossary over the same chapters."""
    reference = {key: entry for key, entry in reference_glossary.items()
                 if start <= int(entry.get("first_chapter") or 0) <= end}
    chosen = {key: entry for key, entry in settled_terms.items() if start <= entry["first_chapter"] <= end}
    shared = sorted(set(chosen) & set(reference))
    agree = [key for key in shared if {value.casefold() for value in renderings(chosen[key])}
             & {value.casefold() for value in renderings(reference[key])}]
    return {"chapters": [start, end], "batch_terms": len(chosen), "sequential_terms": len(reference),
            "shared": len(shared), "same_english_on_shared": len(agree),
            "batch_only": sorted(set(chosen) - set(reference)),
            "sequential_only": sorted(set(reference) - set(chosen)),
            "different_english": [(key, renderings(chosen[key]), renderings(reference[key]))
                                  for key in shared if key not in agree]}


def write_report(report, destination):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.with_suffix(".json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [f"# Batch rehearsal: {report['novel']}, chapters {report['start_chapter']}–{report['end_chapter']}", "",
             "| | Sequential | Batch |", "|---|---|---|"]
    seq, batch = report["arms"]["seq"], report["arms"]["batch"]

    def rate(arm):
        value = arm["own_term_consistency"]
        return f"{value['hits']}/{value['uses']} ({value['rate']:.0%})" if value["rate"] is not None else "n/a"

    lines += [f"| Chapters committed | {seq['committed_chapters']} | {batch['committed_chapters']} |",
              f"| Term consistency (own glossary) | {rate(seq)} | {rate(batch)} |",
              f"| Cost per chapter | ${seq['cost_per_chapter_usd']} | ${batch['cost_per_chapter_usd']} |",
              f"| Seconds per chapter | {seq['seconds_per_chapter']} | {batch['seconds_per_chapter']} |", ""]
    agreement = report["batch_against_sequential_terms"]
    if agreement["rate"] is not None:
        lines.append(f"Batch chapters using the sequential run's renderings: {agreement['hits']}/{agreement['uses']} "
                     f"({agreement['rate']:.0%}).")
    lines += ["", "## Chapters to read for story-state errors", "",
              "Compare who appears, where they are, alive or dead, and their title or role.", ""]
    for number, events in report["story_sample"].items():
        lines.append(f"- [ ] Chapter {number}" + (": " + "; ".join(events) if events else ""))
    destination.with_suffix(".md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return destination.with_suffix(".md")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("prepare", "compare", "terms"))
    parser.add_argument("--novels-root", type=Path, help="where the source novel is (prepare only)")
    parser.add_argument("--out", type=Path, required=True, help="rehearsal folder for the two copies")
    parser.add_argument("--novel", required=True)
    parser.add_argument("--chapters", type=int, default=50)
    parser.add_argument("--max-parallel", type=int, default=32)
    parser.add_argument("--arm", choices=(*ARMS, TERMS_ARM), help="prepare only this copy")
    args = parser.parse_args(argv)
    if args.command == "prepare":
        if args.novels_root is None:
            parser.error("prepare needs --novels-root")
        print(json.dumps(prepare(args.novels_root, args.novel, args.out, args.chapters, args.max_parallel,
                                    (args.arm,) if args.arm else ARMS), indent=2))
        return 0
    _require_rehearsal(args.out)
    if args.command == "terms":
        settled = _read(arm_root(args.out, TERMS_ARM, args.novel) / "data" / "batch"
                        / f"{_read(arm_root(args.out, TERMS_ARM, args.novel) / 'data' / 'rehearsal.json')['start_chapter']:04d}"
                        / "settled.json")["settled"]
        sequential = arm_root(args.out, "seq", args.novel)
        end = min(_read(sequential / "data" / "progress.json")["last_completed"],
                  _read(sequential / "data" / "rehearsal.json")["end_chapter"])
        start = _read(sequential / "data" / "rehearsal.json")["start_chapter"]
        result = term_selection(settled, _read(sequential / "data" / "glossary.json")["entries"], start, end)
        path = args.out / "reports" / f"batch_terms_{args.novel}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({key: value for key, value in result.items() if not isinstance(value, list)
                          or key == "chapters"}, indent=2))
        print(path)
        return 0
    report = compare(args.out, args.novel)
    print(write_report(report, args.out / "reports" / f"batch_rehearsal_{args.novel}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
