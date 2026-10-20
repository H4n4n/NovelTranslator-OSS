from pathlib import Path

import audit_semantic_integrity as cli


def _minimal_report():
    return {
        "novel_id": "xianxia_dragon_heart",
        "title": "Xianxia Cultivation with a Dragon Heart",
        "requested_range": {"start": None, "end": None},
        "selected_chapter_count": 0,
        "audited_chapter_count": 0,
        "source_backed_identity_observations": 0,
        "diagnostic_count": 0,
        "chapters_with_diagnostic_count": 0,
        "diagnostic_types": {},
        "diagnostic_categories": {},
        "diagnostics": [],
        "advisory_count": 0,
        "chapters_with_advisory_count": 0,
        "advisory_types": {},
        "advisory_categories": {},
        "advisories": [],
        "experimental_signal_count": 0,
        "chapters_with_experimental_signal_count": 0,
        "experimental_signal_types": {},
        "experimental_signal_categories": {},
        "experimental_signals": [],
        "suppressed_experimental_signal_count": 0,
        "strict_blocker_count": 0,
        "chapters_with_strict_blocker_count": 0,
        "drift_term_count": 0,
        "drift": [],
        "errors": [],
        "api_calls": 0,
        "writes_performed": 0,
    }


def test_parser_accepts_external_repo_root():
    args = cli.build_parser().parse_args([
        "--novel",
        "xianxia_dragon_heart",
        "--repo-root",
        r"D:\NovelTranslator_DeepSeekFlash",
        "--limit",
        "40",
    ])
    assert args.novel == "xianxia_dragon_heart"
    assert args.repo_root == Path(r"D:\NovelTranslator_DeepSeekFlash")
    assert args.limit == 40
    assert args.experimental_substitutions is False


def test_parser_can_explicitly_enable_experimental_substitutions():
    args = cli.build_parser().parse_args([
        "--novel",
        "xianxia_dragon_heart",
        "--experimental-substitutions",
    ])
    assert args.experimental_substitutions is True


def test_main_passes_external_repo_root_without_mutating_project(monkeypatch):
    captured = {}
    project = object()
    root = Path(r"D:\NovelTranslator_DeepSeekFlash")

    def fake_load(novel_id, repo_root=None):
        captured["novel_id"] = novel_id
        captured["repo_root"] = repo_root
        return project

    def fake_audit(value, start=None, end=None, *, include_experimental=False):
        captured["include_experimental"] = include_experimental
        return _minimal_report()

    snapshots = iter(({"before": 1}, {"before": 1}))
    monkeypatch.setattr(cli, "load_novel_project", fake_load)
    monkeypatch.setattr(cli, "protected_state_snapshot", lambda value: next(snapshots))
    monkeypatch.setattr(cli, "audit_committed_project", fake_audit)
    monkeypatch.setattr(cli, "_print_human", lambda report, limit: None)

    result = cli.main([
        "--novel",
        "xianxia_dragon_heart",
        "--repo-root",
        str(root),
    ])

    assert result == 0
    assert captured == {
        "novel_id": "xianxia_dragon_heart",
        "repo_root": root,
        "include_experimental": False,
    }


def test_main_forwards_explicit_experimental_opt_in(monkeypatch):
    captured = {}
    project = object()

    monkeypatch.setattr(cli, "load_novel_project", lambda novel_id: project)
    monkeypatch.setattr(cli, "protected_state_snapshot", lambda value: {"same": 1})

    def fake_audit(value, start=None, end=None, *, include_experimental=False):
        captured["include_experimental"] = include_experimental
        return _minimal_report()

    monkeypatch.setattr(cli, "audit_committed_project", fake_audit)
    monkeypatch.setattr(cli, "_print_human", lambda report, limit: None)

    result = cli.main([
        "--novel",
        "fixture",
        "--experimental-substitutions",
    ])

    assert result == 0
    assert captured["include_experimental"] is True
