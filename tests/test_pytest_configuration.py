from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_pytest_configuration_does_not_force_repository_shared_basetemp():
    config = (REPO_ROOT / "pytest.ini").read_text(encoding="utf-8")

    assert "--basetemp" not in config
    assert ".pytest_tmp" not in config
