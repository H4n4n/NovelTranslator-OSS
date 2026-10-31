"""Focused tests for the publication guard's inventory, path and secret checks."""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
HEADER = "# header one\n# header two\n"


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / "scripts").mkdir()
    for name in ("publication_guard.py", "guard_allowlist.py"):
        shutil.copy(REPO / "scripts" / name, tmp_path / "scripts" / name)
    (tmp_path / "public_inventory.txt").write_text(HEADER + "stale.py\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("print(1)\n", encoding="utf-8")
    (tmp_path / "a.py").write_text("print(2)\n", encoding="utf-8")
    return tmp_path


def _run(repo, *args):
    return subprocess.run([sys.executable, str(repo / "scripts" / "publication_guard.py"), *args], cwd=repo, capture_output=True, text=True)


def test_regenerate_inventory_sorted_with_header_kept(repo):
    _git(repo, "add", "public_inventory.txt", "b.py", "a.py")
    result = _run(repo, "--regenerate-inventory")
    assert result.returncode == 0, result.stdout
    text = (repo / "public_inventory.txt").read_text(encoding="utf-8")
    assert text == HEADER + "a.py\nb.py\npublic_inventory.txt\n"
    _git(repo, "add", "public_inventory.txt")
    assert _run(repo, "--index").returncode == 0


def _index_with_inventory(repo, *paths):
    listed = sorted({"public_inventory.txt", "scripts/publication_guard.py", "scripts/guard_allowlist.py", *paths})
    (repo / "public_inventory.txt").write_text(HEADER + "\n".join(listed) + "\n", encoding="utf-8")
    _git(repo, "add", "-f", *listed)


def test_index_passes_only_when_inventory_matches(repo):
    _index_with_inventory(repo, "a.py")
    assert _run(repo, "--index").returncode == 0
    _git(repo, "add", "b.py")
    assert _run(repo, "--index").returncode != 0


def test_index_rejects_forbidden_private_path_even_if_listed(repo):
    (repo / "novels").mkdir()
    (repo / "novels" / "x.py").write_text("1\n", encoding="utf-8")
    _index_with_inventory(repo, "novels/x.py")
    assert _run(repo, "--index").returncode != 0


def test_index_rejects_secret_even_if_listed(repo):
    (repo / "k.py").write_text("key = 'sk-" + "a" * 30 + "'\n", encoding="utf-8")
    _index_with_inventory(repo, "k.py")
    result = _run(repo, "--index")
    assert result.returncode != 0
    assert "sk-" + "a" * 30 not in result.stdout + result.stderr


def test_index_rejects_missing_inventory(repo):
    _git(repo, "add", "a.py")
    assert _run(repo, "--index").returncode != 0


def test_regenerate_inventory_refuses_forbidden_path_and_secret(repo):
    (repo / "novels").mkdir()
    (repo / "novels" / "x.py").write_text("1\n", encoding="utf-8")
    _git(repo, "add", "-f", "public_inventory.txt", "novels/x.py")
    before = (repo / "public_inventory.txt").read_text(encoding="utf-8")
    assert _run(repo, "--regenerate-inventory").returncode == 1
    assert (repo / "public_inventory.txt").read_text(encoding="utf-8") == before
    _git(repo, "rm", "-q", "--cached", "novels/x.py")
    (repo / "k.py").write_text("key = 'ghp_" + "a" * 30 + "'\n", encoding="utf-8")
    _git(repo, "add", "k.py")
    assert _run(repo, "--regenerate-inventory").returncode == 1
    assert (repo / "public_inventory.txt").read_text(encoding="utf-8") == before
