"""CI test selection: every non-excluded module runs once; excluded never run."""
import json
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import split_tests  # noqa: E402


def _all_modules():
    return {p.relative_to(REPO).as_posix() for p in (REPO / "tests").glob("test_*.py")}


def test_every_non_excluded_module_in_exactly_one_shard():
    excluded = set(split_tests.load_exclusions()["excluded"])
    flat = [m for shard in split_tests.shards(4) for m in shard]
    assert len(flat) == len(set(flat))
    assert set(flat) == _all_modules() - excluded


def test_excluded_modules_exist_and_never_run():
    excluded = set(split_tests.load_exclusions()["excluded"])
    assert excluded <= _all_modules()
    flat = {m for shard in split_tests.shards(4) for m in shard}
    assert not flat & excluded


def test_deselect_nodes_have_reasons():
    data = split_tests.load_exclusions()
    assert set(data["deselect"]) == set(data["deselect_reason"])


def test_new_test_file_is_picked_up_without_other_changes(tmp_path):
    (tmp_path / "tests").mkdir()
    shutil.copy(REPO / "tests" / "ci_exclusions.json", tmp_path / "tests")
    (tmp_path / "tests" / "test_old.py").write_text("x = 1\n", encoding="utf-8")
    excluded = json.loads((tmp_path / "tests" / "ci_exclusions.json").read_text(encoding="utf-8"))["excluded"]
    (tmp_path / "tests" / excluded[0].split("/")[1]).write_text("x = 1\n", encoding="utf-8")
    before = split_tests.selected_modules(tmp_path)
    assert before == ["tests/test_old.py"]
    (tmp_path / "tests" / "test_brand_new.py").write_text("x = 2\n", encoding="utf-8")
    flat = [m for shard in split_tests.shards(3, tmp_path) for m in shard]
    assert sorted(flat) == ["tests/test_brand_new.py", "tests/test_old.py"]
