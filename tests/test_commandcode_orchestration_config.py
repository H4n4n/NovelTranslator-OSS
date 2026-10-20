from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CC = ROOT / ".commandcode"
AGENTS = CC / "agents"
DEEPSEEK = "deepseek/deepseek-v4.1-flash"


def _frontmatter(path: Path) -> tuple[dict[str, str], str]:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path} must start with YAML frontmatter"
    _empty, header, body = text.split("---", 2)
    values: dict[str, str] = {}
    for raw in header.strip().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, value = line.split(":", 1)
        values[key.strip()] = value.strip().strip('"')
    return values, body.strip()


def _tools(meta: dict[str, str]) -> set[str]:
    return {item.strip() for item in meta["tools"].split(",") if item.strip()}


def test_commandcode_project_settings_are_fail_closed():
    settings = json.loads((CC / "settings.json").read_text(encoding="utf-8"))

    assert settings["model"] == DEEPSEEK
    assert settings["effort"] == "high"
    assert settings["tasteLearning"] is False

    permissions = settings["permissions"]
    assert permissions["defaultMode"] == "default"
    assert permissions["disableBypass"] is True

    deny = set(permissions["deny"])
    ask = set(permissions["ask"])

    assert "Read(.env*)" in deny
    assert "Edit(.env*)" in deny
    assert "Read(**/*.dpapi)" in deny
    assert "Read(**/NovelTranslator/Secrets/**)" in deny
    assert "Edit(**/NovelTranslator/Secrets/**)" in deny
    assert "Edit(.git/**)" in deny
    assert "Shell(git push --force*)" in deny
    assert "Shell(git reset --hard*)" in deny
    assert "Shell(git clean -*)" in deny
    assert "Shell(git push:*)" in ask

    allow = set(permissions["allow"])
    assert (
        r"Shell(D:\\NovelTranslator\\.venv\\Scripts\\python.exe -m pytest:*)"
        in allow
    )
    assert (
        r"Shell(D:\\NovelTranslator\\.venv\\Scripts\\python.exe -m py_compile:*)"
        in allow
    )
    assert not any("-c" in rule for rule in allow if "python.exe" in rule)


def test_commandcode_local_settings_are_gitignored():
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".commandcode/settings.local.json" in ignore


def test_architect_is_deepseek_high_and_read_only_plan():
    meta, body = _frontmatter(AGENTS / "nt-architect.md")
    tools = _tools(meta)

    assert meta["name"] == "nt-architect"
    assert meta["model"] == DEEPSEEK
    assert meta["reasoningEffort"] == "high"
    assert meta["permissionMode"] == "plan"
    assert "edit_file" not in tools
    assert "write_file" not in tools
    assert "IMPLEMENTATION_CONTRACT" in body
    assert "fresh read-only context" in body


def test_primary_engineer_is_deepseek_and_can_edit():
    meta, body = _frontmatter(AGENTS / "nt-engineer-deepseek.md")
    tools = _tools(meta)

    assert meta["model"] == DEEPSEEK
    assert meta["reasoningEffort"] == "high"
    assert meta["permissionMode"] == "auto-accept"
    assert {"edit_file", "write_file", "shell_command"} <= tools
    assert "ENGINEER_BLOCKED" in body


def test_reviewer_is_deepseek_high_read_only_plan():
    meta, body = _frontmatter(AGENTS / "nt-reviewer-deepseek.md")
    tools = _tools(meta)

    assert meta["name"] == "nt-reviewer-deepseek"
    assert meta["model"] == DEEPSEEK
    assert meta["reasoningEffort"] == "high"
    assert meta["permissionMode"] == "plan"
    assert "edit_file" not in tools
    assert "write_file" not in tools
    assert "REVIEW_PASS" in body
    assert "REVIEW_REWORK" in body
    assert "fresh read-only context" in body


def test_glm_commandcode_agents_are_retired():
    assert not (AGENTS / "nt-engineer-glm.md").exists()
    assert not (AGENTS / "nt-reviewer-glm.md").exists()


def test_nt_dev_command_binds_the_deepseek_only_pipeline():
    body = (CC / "commands" / "nt-dev.md").read_text(encoding="utf-8")

    assert "$ARGUMENTS" in body
    assert "read that file completely" in body
    assert "preferred Windows-safe invocation" in body
    assert "nt-architect" in body
    assert "nt-engineer-deepseek" in body
    assert "nt-reviewer-deepseek" in body
    assert "nt-engineer-glm" not in body
    assert "nt-reviewer-glm" not in body
    assert "Do not commit before REVIEW_PASS" in body
    assert "read-only/no-commit/no-push" in body
    assert "Do not use GLM" in body


def test_project_memory_advertises_single_entry_point():
    body = (ROOT / "AGENTS.md").read_text(encoding="utf-8")

    assert "## Command Code DeepSeek-Only Engineering Workflow" in body
    assert "/nt-dev <engineering objective>" in body
    assert "engineering authorization does not authorize" in body.lower()
    assert "nt-reviewer-deepseek" in body


def test_active_commandcode_orchestration_is_deepseek_only():
    active_paths = [
        CC / "settings.json",
        CC / "WORKFLOW.md",
        CC / "commands" / "nt-dev.md",
        AGENTS / "nt-architect.md",
        AGENTS / "nt-engineer-deepseek.md",
        AGENTS / "nt-reviewer-deepseek.md",
    ]
    for path in active_paths:
        text = path.read_text(encoding="utf-8").lower()
        assert "z-ai/glm-5.3-flash" not in text, path
        assert "gpt-5.6-sol" not in text, path
        assert "nt-reviewer-sol" not in text, path

    for agent_path in (
        AGENTS / "nt-architect.md",
        AGENTS / "nt-engineer-deepseek.md",
        AGENTS / "nt-reviewer-deepseek.md",
    ):
        meta, _body = _frontmatter(agent_path)
        assert meta["model"] == DEEPSEEK
        assert meta["reasoningEffort"] == "high"
