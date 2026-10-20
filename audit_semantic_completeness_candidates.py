"""Read-only source-only inventory for broader Chinese explanatory parentheticals.

This command is exploratory calibration, not production QA. It scans committed
Chinese-source chapters for parenthetical spans that begin with a conservative
set of definitional/alias cues, reports which candidates are already covered by
``explicit-source-gloss-v1``, and surfaces additional cue families worth manual
inspection before changing the live completeness policy.

It never reads credentials, calls a provider, or writes project data.
"""

import argparse
import json
import re
from collections import Counter
from pathlib import Path

from chapter_library import index_chapter_library
from novel_project import load_novel_project
from semantic_completeness import explicit_source_glosses
from semantic_integrity_audit import protected_state_snapshot
from source_language_filter import source_language_matches


INVENTORY_MODE = "semantic_completeness_candidate_inventory"

_CJK = r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
_PARENTHESES_RE = re.compile(
    rf"(?P<label>[{_CJK}]{{1,20}})[（(](?P<body>[^）)\r\n]{{2,180}})[）)]"
)
_CUE_PATTERNS = (
    (
        "equivalence",
        re.compile(
            r"^\s*[：:，,、]?\s*(?:"
            r"即(?:是|為|为|指|所謂|所谓)?|"
            r"也就是(?:說|说)?|"
            r"換言之|换言之|"
            r"意即|意為|意为|意思是"
            r")"
        ),
    ),
    (
        "reference",
        re.compile(r"^\s*[：:，,、]?\s*(?:是指|指的是)"),
    ),
    (
        "alias",
        re.compile(
            r"^\s*[：:，,、]?\s*(?:"
            r"又稱|又称|又名|俗稱|俗称|"
            r"簡稱|简称|全稱|全称|別稱|别称"
            r")"
        ),
    ),
)
_LABEL_INTRO_MARKERS = (
    "叫做", "稱為", "称为", "所謂", "所谓", "作為", "作为",
    "做", "是", "為", "为", "當", "当",
)


class SemanticCompletenessCandidateInventoryError(RuntimeError):
    pass


def _read_json(path, label):
    path = Path(path)
    if not path.is_file():
        raise SemanticCompletenessCandidateInventoryError(
            f"{label} does not exist: {path}"
        )
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise SemanticCompletenessCandidateInventoryError(
            f"cannot read {label}: {path}: {exc}"
        ) from exc


def _clean_label(raw):
    raw = str(raw or "")
    best = raw
    best_position = -1
    for marker in _LABEL_INTRO_MARKERS:
        position = raw.rfind(marker)
        candidate = raw[position + len(marker):] if position >= 0 else ""
        if position >= best_position and 1 <= len(candidate) <= 10:
            best = candidate
            best_position = position
    return best


def _cue_family(body):
    for family, pattern in _CUE_PATTERNS:
        if pattern.search(body or ""):
            return family
    return ""


def broader_parenthetical_candidates(source_text):
    """Return source-only parenthetical explanation candidates by cue family."""
    source_text = source_text or ""
    current = explicit_source_glosses(source_text)
    current_spans = [(item["start"], item["end"]) for item in current]
    candidates = []
    for match in _PARENTHESES_RE.finditer(source_text):
        family = _cue_family(match.group("body"))
        if not family:
            continue
        start, end = match.start(), match.end()
        covered = any(
            (a <= start and end <= b) or (start <= a and b <= end)
            for a, b in current_spans
        )
        candidates.append({
            "label": _clean_label(match.group("label")),
            "label_context": match.group("label"),
            "body": match.group("body"),
            "cue_family": family,
            "start": start,
            "end": end,
            "covered_by_v1": covered,
        })
    return tuple(candidates)


def inventory_project(novel_id, *, repo_root=None):
    project = load_novel_project(novel_id, repo_root=repo_root)
    if not source_language_matches(project.config.source_language, "zh"):
        raise SemanticCompletenessCandidateInventoryError(
            "candidate inventory currently supports Chinese source projects only: "
            f"{novel_id} ({project.config.source_language})"
        )
    before = protected_state_snapshot(project)
    chapters = _read_json(project.chapters_file, "chapter library")
    progress = _read_json(project.progress_file, "progress")
    if not isinstance(chapters, list) or not isinstance(progress, dict):
        raise SemanticCompletenessCandidateInventoryError(
            "chapter library must be a list and progress must be an object"
        )
    chapter_map = index_chapter_library(
        chapters,
        project.config.final_chapter,
        project.config.chapter_numbering,
    )
    completed = progress.get("completed", [])
    if not isinstance(completed, list):
        raise SemanticCompletenessCandidateInventoryError("progress.completed must be a list")

    findings = []
    chapters_scanned = 0
    for chapter in sorted({
        value for value in completed
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
    }):
        entry = chapter_map.get(chapter)
        if not isinstance(entry, dict):
            continue
        chapters_scanned += 1
        for item in broader_parenthetical_candidates(entry.get("text", "")):
            findings.append({"chapter": chapter, **dict(item)})

    unchanged = before == protected_state_snapshot(project)
    families = Counter(item["cue_family"] for item in findings)
    covered = sum(1 for item in findings if item["covered_by_v1"])
    return {
        "mode": INVENTORY_MODE,
        "status": "PASS" if unchanged else "FAIL",
        "novel_id": project.config.id,
        "title": project.config.title,
        "source_language": project.config.source_language,
        "chapters_scanned": chapters_scanned,
        "candidate_count": len(findings),
        "covered_by_v1_count": covered,
        "additional_candidate_count": len(findings) - covered,
        "cue_family_counts": dict(sorted(families.items())),
        "findings": findings,
        "api_calls": 0,
        "project_writes": 0,
        "protected_state_unchanged": unchanged,
    }


def inventory_all(repo_root, *, source_language="zh"):
    repo_root = Path(repo_root).resolve()
    novels_root = repo_root / "novels"
    reports = []
    discovered = 0
    for directory in sorted(novels_root.iterdir() if novels_root.is_dir() else []):
        config_path = directory / "novel.json"
        if not directory.is_dir() or not config_path.is_file():
            continue
        try:
            config = _read_json(config_path, "novel config")
        except SemanticCompletenessCandidateInventoryError:
            continue
        if (
            not isinstance(config, dict)
            or not source_language_matches(config.get("source_language"), source_language)
        ):
            continue
        novel_id = config.get("id")
        if not isinstance(novel_id, str):
            continue
        discovered += 1
        reports.append(inventory_project(novel_id, repo_root=repo_root))

    families = Counter()
    for report in reports:
        families.update(report["cue_family_counts"])
    findings = [
        {"novel_id": report["novel_id"], **item}
        for report in reports
        for item in report["findings"]
    ]
    return {
        "mode": INVENTORY_MODE + "_all",
        "status": "PASS" if all(report["status"] == "PASS" for report in reports) else "FAIL",
        "source_language": source_language,
        "novels_discovered": discovered,
        "novels_scanned": len(reports),
        "chapters_scanned": sum(report["chapters_scanned"] for report in reports),
        "candidate_count": sum(report["candidate_count"] for report in reports),
        "covered_by_v1_count": sum(report["covered_by_v1_count"] for report in reports),
        "additional_candidate_count": sum(report["additional_candidate_count"] for report in reports),
        "cue_family_counts": dict(sorted(families.items())),
        "reports": reports,
        "findings": findings,
        "api_calls": 0,
        "project_writes": 0,
        "protected_state_unchanged": all(
            report["protected_state_unchanged"] for report in reports
        ),
    }


def _print_human(report, *, sample_limit=20):
    print("=" * 70)
    print("SEMANTIC COMPLETENESS — BROADER SOURCE CANDIDATE INVENTORY")
    print("=" * 70)
    print(f"Status: {report['status']}")
    if "novels_discovered" in report:
        print(f"Source language filter: {report['source_language']}")
        print(f"Novels discovered: {report['novels_discovered']}")
        print(f"Novels scanned: {report['novels_scanned']}")
    else:
        print(f"Novel: {report['novel_id']} — {report['title']}")
        print(f"Source language: {report['source_language']}")
    print(f"Chapters scanned: {report['chapters_scanned']}")
    print(f"Broader definitional parenthetical candidates: {report['candidate_count']}")
    print(f"Already covered by v1: {report['covered_by_v1_count']}")
    print(f"Additional candidates beyond v1: {report['additional_candidate_count']}")
    print("Cue families: " + (
        ", ".join(f"{key}={value}" for key, value in report["cue_family_counts"].items())
        or "none"
    ))
    if report.get("findings"):
        print(f"Sample candidates (up to {sample_limit}):")
        for item in report["findings"][:sample_limit]:
            prefix = f"{item.get('novel_id')} / " if item.get("novel_id") else ""
            body = str(item.get("body", "")).replace("\n", " ")[:80]
            print(
                f"  {prefix}Chapter {item['chapter']}: {item['cue_family']} | "
                f"label={item['label']!r} | v1={'YES' if item['covered_by_v1'] else 'NO'} | "
                f"body={body!r}"
            )
    print("API calls: 0")
    print("Project writes: 0")
    print(
        "Protected production state unchanged: "
        + ("YES" if report["protected_state_unchanged"] else "NO")
    )
    print("=" * 70)


def build_parser():
    parser = argparse.ArgumentParser(
        description="Inventory broader Chinese definitional parentheticals without changing QA policy."
    )
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--novel", default=None)
    parser.add_argument("--source-language", default="zh")
    parser.add_argument("--sample-limit", type=int, default=20)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        report = (
            inventory_project(args.novel, repo_root=args.repo_root)
            if args.novel
            else inventory_all(args.repo_root, source_language=args.source_language)
        )
    except Exception as exc:
        if args.json:
            print(json.dumps({
                "mode": INVENTORY_MODE,
                "status": "FAIL",
                "api_calls": 0,
                "project_writes": 0,
                "error": str(exc),
            }, ensure_ascii=False, indent=2))
        else:
            print("=" * 70)
            print("SEMANTIC COMPLETENESS — BROADER SOURCE CANDIDATE INVENTORY")
            print("=" * 70)
            print("Status: FAIL")
            print(f"Error: {exc}")
            print("API calls: 0")
            print("Project writes: 0")
            print("=" * 70)
        return 2
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_human(report, sample_limit=max(args.sample_limit, 0))
    return 0 if report["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
